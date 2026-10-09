# hubspot-audit

Read-only HubSpot portal audit extractor for Process Pro Consulting. It pulls a prospect's portal with a read-only Service Key or by installing our public app (OAuth), computes aggregate metrics locally, and writes an audit bundle that Claude turns into the internal audit report.

Status: v0.1.0. Built and unit-tested against a fake portal. **Not yet run against a live HubSpot portal**; see [Verification status](#verification-status).

## How it works

1. The prospect creates a read-only Service Key and fills in the screenshot checklist (`prospect/`).
2. `hubspot-audit run` probes the key, pulls ten categories, and writes `output/<prospect>_<date>/`.
3. Claude reads `audit_bundle.json` plus the intake and checklist using `prompts/bundle_audit_prompt.md` and writes the report.

Claude sees aggregates (for example, "Job Title populated on 23% of sampled contacts"), not names or emails. Contact records are held in memory only.

## Install

Requires Python 3.11+.

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
| `HUBSPOT_SERVICE_KEY` | Read-only Service Key for ad hoc runs. Configured clients use their own `service_key_env`. Read only from the environment; never written or logged. |
| `PROSPECT_NAME` | Name for ad hoc runs without a config entry. |
| `HUBSPOT_AUDIT_CONFIG` | Path to the client list (default `./clients.toml`). |
| `ERROR_ROUTER_URL` | Central Error Router webhook. **Required**; the run refuses to start without it. |
| `FLOW_ID`, `FLOW_NAME` | Identify this tool in error payloads. |
| `PLATFORM_NAME` | Must be `LOCAL_SCRIPT` for this tool. |

## Clients

Keep the list of audited portals in `clients.toml` (gitignored; start from `clients.example.toml`, or point `HUBSPOT_AUDIT_CONFIG` at another file). The file never holds credentials: a Service Key client names the environment variable that holds its key, and an OAuth client is identified by portal ID with its token held by the receiver. A file containing something that looks like a HubSpot token is refused.

```toml
[defaults]
categories = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
sample_size = 1000
output_dir = "output"

[[clients]]
name = "Acme Co"            # shown in the bundle and used for the output folder (slug: acme-co)
portal_id = "12345678"      # OAuth clients: add after the first install
auth = "oauth"              # default; or "service-key" with service_key_env = "ACME_HUBSPOT_KEY"
categories = [1, 3, 6]      # optional per-client overrides: categories, sample_size
enabled = true              # false = skipped by `run --all`
```

Validation rejects unknown keys (typos, and any `service_key`-style field), bad categories, duplicate names, slugs or portal IDs, and a Service Key client without `service_key_env`.

| Command | What it does |
| --- | --- |
| `hubspot-audit check-config` | Validate the file |
| `hubspot-audit clients` | Table of configured clients with install status and last use from the receiver; flags installed portals missing from the config and configured OAuth clients that are not installed |
| `hubspot-audit run --client acme-co` | Run one client (slug, name or portal ID) |
| `hubspot-audit run --all` | Run every enabled client in file order. A failing client is reported to the error router and the batch continues; the command exits non-zero at the end if any failed |

Flags (`--categories`, `--sample-size`, `--output-dir`) override the config. A run refuses to continue if the token's portal does not match the configured `portal_id`.

First run for a new OAuth client: add the client with no `portal_id`, run `hubspot-audit run --client acme-co --state <state>` (within 24 hours of the install), and the CLI prints the `portal_id` line to add to the config.

## Run

```bash
hubspot-audit run --client acme-co         # a configured client
hubspot-audit run --all                    # every enabled client
hubspot-audit run --prospect "Acme Co"     # ad hoc, Service Key from HUBSPOT_SERVICE_KEY
hubspot-audit run --prospect "Acme Co" --categories 1,3 --sample-size 500
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

Tokens are kept in memory for the run and never written to disk. The token calls use HubSpot's dated `2026-03` OAuth endpoints (`/oauth/v1/*` is retired on 2027-02-16). In this mode the bundle's `meta.oauth_scopes_granted` lists the scopes HubSpot actually granted (more reliable than the probe-based `scopes_granted`).

### Hosted receiver (Netlify)

A prospect or client installs the app on their own. The Netlify receiver in `netlify/` catches HubSpot's redirect, exchanges the code, encrypts the refresh token to our public key and **keeps** the ciphertext in Netlify Blobs, keyed by portal ID, so audits can be rerun (for example monthly) without another install. A reinstall replaces the stored token.

```
client admin --install link--> HubSpot --redirect--> Netlify /oauth/callback
                                                        | exchange code, encrypt, store portal/<id>
CLI / scheduler: run --auth oauth --portal ID --> Netlify /api/token --> ciphertext
                 decrypt with private key, hold in memory for the run
```

Receiver endpoints (all need the bearer `TOKEN_FETCH_SECRET`):

| Call | Purpose |
| --- | --- |
| `GET /api/token?portal=ID` or `?state=S` | Read the ciphertext (repeatable; records `last_used`). `state` resolves to a portal for 24 hours after install. |
| `PUT /api/token?portal=ID` | Replace the ciphertext. The CLI does this automatically if HubSpot ever rotates a refresh token, and fails the run if the write-back fails (a lost rotated token would break the next run). |
| `DELETE /api/token?portal=ID` | Offboard a client. |
| `GET /api/clients` | List installed portals: install date, last use, scopes. No token material. |

Setup, once: follow `DEPLOY.md`. In short, `hubspot-audit setup --site-url https://<site>.netlify.app` generates the private key (mode 600; back it up, because without it stored tokens cannot be read), the two shared secrets, `.env.receiver` and the HubSpot app definition in `hubspot-app/`; `scripts/netlify_env.sh` pushes the variables to Netlify scoped to Production; `netlify deploy --prod` ships the functions; `hubspot-audit receiver-check` smoke-tests them. Any machine that runs scheduled audits needs the private key, `TOKEN_RECEIVER_URL`, `TOKEN_FETCH_SECRET`, `STATE_SIGNING_SECRET` and the HubSpot client ID and secret.

Per client:

1. `hubspot-audit install-url` prints a signed install link and a state value. Send the link with `prospect/install_instructions_oauth.md`.
2. After they install: `hubspot-audit run --auth oauth --state <state>` (first run, within 24 hours), then `--portal <hub id>` for every later run. `hubspot-audit clients` lists portal IDs and install status.
3. To offboard: `hubspot-audit revoke --portal <id>` revokes the refresh token at HubSpot (best effort; a failure is reported and does not stop the next step), then deletes our stored copy. The client should still uninstall the app to end the connection on their side.

Netlify Blobs stores are shared by every deploy context, so a deploy preview of a pull request would otherwise read production tokens. The function only opens the production store when `TOKEN_STORE_NAME` is set, and every other context uses a separate empty store. Also scope `TOKEN_FETCH_SECRET`, `HUBSPOT_CLIENT_SECRET` and `TOKEN_PUBLIC_KEY_PEM` to Production, so previews cannot run the flow at all.

Security properties: the state is HMAC-signed so the receiver rejects installs we did not create; Netlify holds only RSA-OAEP ciphertext, readable only with our private key; every receiver call needs the bearer secret; no codes, tokens or HubSpot response bodies are logged or sent to the error router. During the callback the function briefly holds the plaintext token and client secret in memory.

What persistent storage means: we hold long-lived read access to each client's portal until they uninstall or we revoke. The exposure is the Netlify secrets plus the private key together (either alone is not enough). Treat the private key and `TOKEN_FETCH_SECRET` as production credentials, rotate them if a laptop is lost, and keep the per-client revoke path working.

Not built yet: the monthly scheduler (it needs a runner that holds the private key and the receiver secrets) and a history of bundles to compare month over month.

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

## Verification log

What was checked, how, and what is still open. "Docs" means HubSpot or Netlify documentation as surfaced by web search from the build sandbox, which could not open the docs sites directly; treat it as good evidence, not a substitute for the first live run.

**Confirmed against the Process Pro portal** (read through the HubSpot connector; property existence, types and options only, not the REST endpoints):

- These property names exist with the expected types: contacts `hs_analytics_source` (values include `DIRECT_TRAFFIC` and `OFFLINE`), `hs_analytics_source_data_1`, `hs_email_optout`, `hubspot_owner_id`, `notes_last_updated`, `num_contacted_notes`, `lifecyclestage`, `hs_lead_status`, `createdate`; deals `hs_is_closed`, `notes_last_updated`, `hs_manual_forecast_category`, `dealtype` (`newbusiness`, `existingbusiness`), `closedate`, `hs_lastmodifieddate`, `dealstage`, `pipeline`, `amount`.
- **Fixed:** `hs_object_source` does not exist. The record-source property is `hs_object_source_label`. Category 10 now uses it, with manual/offline sources `CRM_UI`, `CRM_UI_BULK_ACTION`, `IMPORT` and `BATCH_UPDATE`.
- On that portal "Existing Business" is a **pipeline** while `dealtype` has "Existing Customer", so the renewals proxy (open `existingbusiness` deals closing in 60 days) will under-count there. Portals model renewals differently; the metric note says so.

**Confirmed in docs:**

- `http://localhost` is accepted as a redirect URI for testing, but IP addresses are not, so the loopback flow now requires `http://localhost:<port>` and rejects `127.0.0.1`. Production redirects must be HTTPS (the Netlify callback is).
- v1 OAuth endpoints stay up until 2027-02-16. Code now uses `/oauth/2026-03/token`, `/token/introspect` (POST, form body, token not in the URL) and `/token/revoke`. The token endpoint returns the same response as v1, and `hub_id` and `scopes` are now returned with the token, so the introspect call is only a fallback.
- Access tokens last about 30 minutes. HubSpot says refresh tokens do not expire unless the app is uninstalled or the token is revoked; the rotation write-back stays as a guard.
- Install limits: an OAuth app distributed through the marketplace and not yet listed can be installed in **25** accounts; privately distributed apps in **10** allowlisted accounts; a listed app has no limit. Listing needs at least three active installs in unaffiliated production accounts (the number has changed before). Plan for 25 clients before listing.
- Netlify Blobs: `set(key, data, { metadata })`, `getWithMetadata` returning `{ data, etag, metadata }`, `list({ prefix })` returning `{ blobs }`, and `delete` match the calls used. Reads are eventually consistent by default (up to 60 seconds), so the store is opened with `consistency: "strong"`. Stores are shared across deploy contexts (handled above).
- Functions v2 `config` accepts `path` (string or array) and `method` (array).

**Verified locally** (no network): the Netlify handlers run over real HTTP in `netlify/dev-server.mjs` with an in-memory store and a fake HubSpot, driven by the same Python client code the CLI uses: install callback, replay protection, list, fetch by state and by portal, rotation write-back, offboard, and `receiver-check`. This covers our wiring, not Netlify's routing or HubSpot's behavior.

**Still unconfirmed:**

- The exact introspect response schema (fields beyond `hub_id` and `scopes`; whether the token parameter is `token`) and the revoke request parameters. Revoke is best effort for that reason.
- That `consistency: "strong"` is accepted as a `getStore` option in the installed `@netlify/blobs` version. If the first deploy rejects it, pass it per read instead.
- How required versus optional scopes are declared in the current developer platform, who may approve an install, and the Service Key click path and scope names (`prospect/scopes.md`).
- The HubSpot project format used in `hubspot-app/` (written from memory of the 2025.2 layout), the Netlify CLI flags in `scripts/netlify_env.sh`, and `hs project upload` behavior.
- Every REST endpoint behavior listed under Known API gaps, and the Netlify function itself (never deployed).

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
netlify/             OAuth receiver (Netlify Functions, JavaScript), local dev server and tests
hubspot-app/         HubSpot app definition (uploaded with the HubSpot CLI)
scripts/             netlify_env.sh (push receiver variables to Netlify)
DEPLOY.md            Runbook: create the app, deploy, verify
src/hubspot_audit/   client.py, auth.py, receiver.py, metrics.py, context.py, bundle.py, cli.py, categories/
schema/              audit_bundle.schema.json
prompts/             live_audit_prompt.md (reference), bundle_audit_prompt.md
prospect/            setup_instructions.md, scopes.md, screenshot_checklist.md
tests/
```
