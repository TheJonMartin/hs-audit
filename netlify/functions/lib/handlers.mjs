import { randomUUID } from "node:crypto";
import { handleFailure } from "./error_router.mjs";
import { encryptPayload, safeEqual, verifyState } from "./security.mjs";

const HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token";
const HUBSPOT_INTROSPECT_URL = "https://api.hubapi.com/oauth/v1/access-tokens/";
const STATE_LOOKUP_TTL_MS = 24 * 60 * 60 * 1000; // an install link can be exchanged for a portal id for 24h
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
const TOKEN_ENV = ["STATE_SIGNING_SECRET", "TOKEN_FETCH_SECRET", "ERROR_ROUTER_URL"];
const PORTAL_PATTERN = /^\d{1,20}$/;
const portalKey = (id) => `portal/${id}`;
const stateKey = (nonce) => `state/${nonce}`;

/**
 * Create the OAuth callback handler. Exchanges the code, encrypts the refresh token to the
 * Process Pro public key and stores only the ciphertext under the portal id (a reinstall replaces
 * the previous token). Logs no codes, tokens or response bodies.
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
      if (await store.get(stateKey(nonce), { type: "text" })) {
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
      const portal = String(info.hub_id ?? "");
      if (!PORTAL_PATTERN.test(portal)) throw new Error("HubSpot token info did not include a portal id.");
      context.record_id = portal;

      context.process_name = "Encrypting and storing token";
      const stamp = now();
      const ciphertext = encryptPayload({ refresh_token: tokens.refresh_token }, env.TOKEN_PUBLIC_KEY_PEM);
      await store.set(portalKey(portal), ciphertext, { metadata: { stored_at: stamp, installed_at: stamp, last_used: null, scopes: info.scopes ?? [] } });
      await store.set(stateKey(nonce), JSON.stringify({ hub_id: portal }), { metadata: { stored_at: stamp } });
      return page("Connected", "Process Pro Audit is connected. You can close this tab and let your Process Pro contact know.");
    } catch (error) {
      await handleFailure(env, error, context, fetchImpl);
      throw error;
    }
  };
}

/**
 * Create the token-management handler used by the CLI and the scheduled runner.
 *   GET    /api/token?portal=ID | ?state=S   read ciphertext (non-destructive; records last_used)
 *   PUT    /api/token?portal=ID              replace ciphertext (HubSpot rotated the refresh token)
 *   DELETE /api/token?portal=ID              offboard: delete the stored token
 *   GET    /api/clients                      list installed portals (no ciphertext)
 * Every call needs the bearer fetch secret.
 * @param {{env: object, store: object, fetchImpl?: Function, now?: Function}} deps
 */
export function createTokenHandler({ env, store, fetchImpl = fetch, now = Date.now }) {
  requireEnv(env, TOKEN_ENV);
  return async function tokens(request) {
    const context = { process_name: "Authorizing token request", correlation_id: randomUUID() };
    try {
      const bearer = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
      if (!safeEqual(bearer, env.TOKEN_FETCH_SECRET)) return json({ error: "unauthorized" }, 401);
      const url = new URL(request.url);

      if (url.pathname.endsWith("/clients")) {
        context.process_name = "Listing clients";
        const { blobs } = await store.list({ prefix: "portal/" });
        const clients = [];
        for (const { key } of blobs) {
          const entry = await store.getWithMetadata(key, { type: "text" });
          if (entry) clients.push({ hub_id: key.slice("portal/".length), ...entry.metadata });
        }
        return json({ clients });
      }

      const portal = await resolvePortal(url, env, store, now);
      if (portal.error) return json({ error: portal.error }, portal.status);
      context.record_id = portal.id;
      const key = portalKey(portal.id);

      if (request.method === "GET") {
        context.process_name = "Reading stored token";
        const entry = await store.getWithMetadata(key, { type: "text" });
        if (!entry) return json({ error: "not found" }, 404);
        await store.set(key, entry.data, { metadata: { ...entry.metadata, last_used: now() } });
        return json({ hub_id: portal.id, ciphertext: entry.data, scopes: entry.metadata?.scopes ?? [], installed_at: entry.metadata?.installed_at ?? null });
      }
      if (request.method === "PUT") {
        context.process_name = "Replacing stored token";
        const body = await request.json();
        const entry = await store.getWithMetadata(key, { type: "text" });
        if (!entry) return json({ error: "not found" }, 404);
        if (typeof body.ciphertext !== "string" || !body.ciphertext) return json({ error: "ciphertext required" }, 400);
        await store.set(key, body.ciphertext, { metadata: { ...entry.metadata, stored_at: now() } });
        return json({ ok: true });
      }
      if (request.method === "DELETE") {
        context.process_name = "Deleting stored token";
        await store.delete(key);
        return json({ ok: true });
      }
      return json({ error: "method not allowed" }, 405);
    } catch (error) {
      await handleFailure(env, error, context, fetchImpl);
      throw error;
    }
  };
}

/** Resolve `?portal=` or a signed `?state=` (valid 24h after install) to a portal id. */
async function resolvePortal(url, env, store, now) {
  const direct = url.searchParams.get("portal");
  if (direct) return PORTAL_PATTERN.test(direct) ? { id: direct } : { error: "invalid portal", status: 400 };
  const nonce = verifyState(url.searchParams.get("state"), env.STATE_SIGNING_SECRET);
  if (!nonce) return { error: "invalid state", status: 400 };
  const entry = await store.getWithMetadata(stateKey(nonce), { type: "text" });
  if (!entry) return { error: "not found", status: 404 };
  if (now() - (entry.metadata?.stored_at ?? 0) > STATE_LOOKUP_TTL_MS) return { error: "expired", status: 410 };
  return { id: JSON.parse(entry.data).hub_id };
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
