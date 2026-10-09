import { randomUUID } from "node:crypto";
import { handleFailure } from "./error_router.mjs";
import { encryptPayload, safeEqual, verifyState } from "./security.mjs";

const HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token";
const HUBSPOT_INTROSPECT_URL = "https://api.hubapi.com/oauth/v1/access-tokens/";
const TOKEN_TTL_MS = 24 * 60 * 60 * 1000; // stored tokens expire after 24 hours
const HTML = { "Content-Type": "text/html; charset=utf-8" };

const page = (title, body, status = 200) =>
  new Response(`<!doctype html><meta charset="utf-8"><title>${title}</title><body style="font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem"><h2>${title}</h2><p>${body}</p></body>`, { status, headers: HTML });

/**
 * Fail fast when required configuration is missing (standard: do not start half-configured).
 * @throws {Error}
 */
export function requireEnv(env, names) {
  const missing = names.filter((n) => !env[n]);
  if (missing.length) throw new Error(`Missing environment variables: ${missing.join(", ")}`);
}

const CALLBACK_ENV = ["HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET", "OAUTH_REDIRECT_URI", "STATE_SIGNING_SECRET", "TOKEN_PUBLIC_KEY_PEM", "ERROR_ROUTER_URL"];
const FETCH_ENV = ["STATE_SIGNING_SECRET", "TOKEN_FETCH_SECRET", "ERROR_ROUTER_URL"];

/**
 * Create the OAuth callback handler. Exchanges the code, encrypts the refresh token to the
 * Process Pro public key and stores only the ciphertext. Logs no codes, tokens or response bodies.
 * @param {{env: object, store: object, fetchImpl?: Function, now?: Function}} deps
 */
export function createCallbackHandler({ env, store, fetchImpl = fetch, now = Date.now }) {
  requireEnv(env, CALLBACK_ENV);
  return async function callback(request) {
    const context = { process_name: "Validating callback", correlation_id: randomUUID() };
    try {
      const url = new URL(request.url);
      if (url.searchParams.get("error")) {
        return page("Install cancelled", "No access was granted. You can close this tab.", 200);
      }
      const code = url.searchParams.get("code");
      const nonce = verifyState(url.searchParams.get("state"), env.STATE_SIGNING_SECRET);
      if (!code || !nonce) {
        return page("Invalid link", "This install link is not valid. Ask your Process Pro contact for a new one.", 400);
      }
      context.record_id = nonce;
      if (await store.get(nonce, { type: "text" })) {
        return page("Already connected", "This install link has already been used.", 409);
      }

      context.process_name = "Exchanging authorization code";
      const tokens = await postForm(fetchImpl, HUBSPOT_TOKEN_URL, {
        grant_type: "authorization_code",
        client_id: env.HUBSPOT_CLIENT_ID,
        client_secret: env.HUBSPOT_CLIENT_SECRET,
        redirect_uri: env.OAUTH_REDIRECT_URI,
        code,
      });

      context.process_name = "Reading token info";
      const info = await getJson(fetchImpl, HUBSPOT_INTROSPECT_URL + tokens.access_token);

      context.process_name = "Encrypting and storing token";
      const ciphertext = encryptPayload({ refresh_token: tokens.refresh_token }, env.TOKEN_PUBLIC_KEY_PEM);
      await store.set(nonce, ciphertext, { metadata: { stored_at: now(), hub_id: info.hub_id ?? null, scopes: info.scopes ?? [] } });
      return page("Connected", "Process Pro Audit is connected. You can close this tab and let your Process Pro contact know.");
    } catch (error) {
      await handleFailure(env, error, context, fetchImpl);
      throw error;
    }
  };
}

/**
 * Create the one-time token retrieval handler used by the CLI.
 * @param {{env: object, store: object, fetchImpl?: Function, now?: Function}} deps
 */
export function createFetchHandler({ env, store, fetchImpl = fetch, now = Date.now }) {
  requireEnv(env, FETCH_ENV);
  return async function fetchToken(request) {
    const context = { process_name: "Authorizing token fetch", correlation_id: randomUUID() };
    try {
      const bearer = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
      if (!safeEqual(bearer, env.TOKEN_FETCH_SECRET)) return json({ error: "unauthorized" }, 401);
      const nonce = verifyState(new URL(request.url).searchParams.get("state"), env.STATE_SIGNING_SECRET);
      if (!nonce) return json({ error: "invalid state" }, 400);
      context.record_id = nonce;

      context.process_name = "Reading stored token";
      const entry = await store.getWithMetadata(nonce, { type: "text" });
      if (!entry) return json({ error: "not found" }, 404);
      await store.delete(nonce); // one-time read: gone whether or not it has expired
      const storedAt = entry.metadata?.stored_at ?? 0;
      if (now() - storedAt > TOKEN_TTL_MS) return json({ error: "expired" }, 410);
      return json({ ciphertext: entry.data, hub_id: entry.metadata?.hub_id ?? null, scopes: entry.metadata?.scopes ?? [], stored_at: storedAt });
    } catch (error) {
      await handleFailure(env, error, context, fetchImpl);
      throw error;
    }
  };
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function postForm(fetchImpl, url, fields) {
  const response = await fetchImpl(url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(fields), signal: AbortSignal.timeout(10000) });
  // Never include the response body in errors: it can echo the code or tokens.
  if (!response.ok) throw new Error(`HubSpot token exchange failed: HTTP ${response.status}`);
  return response.json();
}

async function getJson(fetchImpl, url) {
  const response = await fetchImpl(url, { signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(`HubSpot token info failed: HTTP ${response.status}`);
  return response.json();
}
