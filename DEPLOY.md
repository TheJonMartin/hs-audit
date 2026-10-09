# Deploy the Process Pro Audit App

## What

Stand up the hosted OAuth receiver on Netlify and register the Process Pro Audit app in HubSpot, so clients can install the app with one click and monthly audits can reuse their stored tokens. The repo generates the keys, secrets, app definition and checks; you run the account-level steps, because they need your Netlify and HubSpot logins.

Why it matters: until this is done, only Service Key audits and on-a-call installs work.

## How

Do the steps in order. The order matters: the HubSpot app needs the Netlify URL, and Netlify needs the app's client secret.

**Before you start.** You need a Netlify account, a HubSpot developer account, Node 22 and Python 3.11. Install the CLIs once:

```bash
npm install -g netlify-cli @hubspot/cli
pip install -e ".[dev]"
```

**1. Create the Netlify site** (empty for now; this fixes the URL).

```bash
SITE=pick-a-unique-name        # your choice; the site URL becomes https://$SITE.netlify.app
netlify login
netlify sites:create --name "$SITE"
netlify link --name "$SITE"
```

Keep the same terminal open for the next steps: they reuse `$SITE`. If you open a new one, set `SITE` again first. Never paste `<angle-bracket>` placeholders into zsh or bash: `<` is a redirect and the command fails with a parse error.

**2. Generate keys and secrets, and point the app definition at the site.**

```bash
hubspot-audit setup --site-url "https://$SITE.netlify.app" --support-email "support@yourcompany.com"
```

This creates the private key (`~/.hubspot-audit/token_private.pem`, mode 600), `STATE_SIGNING_SECRET` and `TOKEN_FETCH_SECRET`, writes `.env.receiver` (Netlify's variables) and adds the shared values to your local `.env`. It prints names only, never values. It is safe to re-run: nothing is rotated or overwritten. **Back up the private key now**; without it, stored client tokens cannot be read.

**3. Register the app in HubSpot.** Edit the `REPLACE-WITH-*` support fields in `hubspot-app/src/app/app-hsmeta.json`, then:

```bash
cd hubspot-app
hs account auth          # choose your developer account
hs project validate      # checks the definition against HubSpot's schemas before anything is uploaded
hs project upload
```

In the developer account, open the app's Auth settings and copy the **Client ID** and **Client secret** into `.env.receiver` (`HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET`). Also set `ERROR_ROUTER_URL` (and `FLOW_ID`) there. If `hs project upload` rejects the definition, create the app in the developer UI instead using the same name, the redirect URL `https://YOUR-SITE.netlify.app/oauth/callback`, and the scopes listed in `src/hubspot_audit/scopes.py` (required and optional). Keep the distribution as marketplace, which allows 25 installs before listing.

**4. Push the variables and deploy.**

```bash
scripts/netlify_env.sh        # refuses to run while anything required is blank
netlify deploy --prod         # from the repo root; netlify.toml sets the build base
```

The script scopes every variable to the Production context, and marks the secrets as secret, so deploy previews cannot read client tokens.

**5. Check the deployment.**

```bash
hubspot-audit receiver-check
```

All five lines should say PASS. A FAIL on "accepts the fetch secret" means `TOKEN_FETCH_SECRET` differs between `.env` and Netlify; any other FAIL means the function is not routed or not running.

**6. Test an install on a portal you control** (start with Process Pro's own).

```bash
hubspot-audit install-url       # prints the install URL and a state value
```

Open the URL as a Super Admin of the portal, approve, and confirm the page says "Connected". Then:

```bash
hubspot-audit clients                                  # the portal ID appears as installed
STATE=paste-the-state-value-printed-above
hubspot-audit run --prospect "Process Pro" --auth oauth --state "$STATE" --categories 1,3
```

Add the portal to `clients.toml` using the `portal_id` the run prints. Later runs use `run --client acme-co` (the client's slug).

**Trying it without deploying.** `PORT=8787 node netlify/dev-server.mjs` (with the variables from `.env.receiver` exported) serves the real functions locally with an in-memory store and a fake HubSpot. It proves the wiring, not HubSpot's behavior. `pytest tests/test_local_receiver.py` does this automatically.

## Done

You are done when all of these are true:

- [ ] `netlify deploy --prod` succeeded and `hubspot-audit receiver-check` prints five PASS lines.
- [ ] The private key is backed up somewhere other than the machine that generated it.
- [ ] A test install on a portal you control shows that portal in `hubspot-audit clients`.
- [ ] `hubspot-audit run --prospect "Process Pro" --auth oauth --portal 12345678` (your portal ID) completes, and the bundle's `meta.oauth_scopes_granted` matches the scopes in the app definition.
- [ ] `hubspot-audit revoke --portal 12345678` removes the portal from `clients`, and a following `run` fails with "not found".
- [ ] In a Netlify deploy preview, `/api/clients` does not return client data (the production store name is not set there).

## What to expect to need fixing

None of the commands below have been run against real accounts from this repository. The likely first-deploy surprises, in order:

1. **HubSpot project format.** The app definition's field names match the `@hubspot/cli` 8.16 type definitions (distribution, auth type, redirect URLs, required, optional and conditionally required scopes, support block), and `platformVersion` is 2026.09, the newest non-beta version that CLI knows. Its schema is fetched from HubSpot at validation time, so nothing could be checked offline. `hs project validate` is the first real check. The CLI's app type also lists a `logo` field; if validation says it is required, add a logo file and reference it. If a field is rejected, fix it in the file or use the UI fallback in step 3.
2. **Netlify CLI flags.** `netlify env:set --context production --secret` is used for scoping and secrecy. If your CLI version rejects a flag, set the variables in the Netlify UI (Site configuration, Environment variables), scoped to Production.
3. **Blobs consistency option.** `consistency: "strong"` in `netlify/functions/lib/store.mjs`. If the function errors on it, remove it and pass it per read instead.
4. **Token introspect and revoke parameters.** Both are best effort; an error there does not block an install or a run.
