import assert from "node:assert/strict";
import crypto from "node:crypto";
import { test } from "node:test";
import { createCallbackHandler, createTokenHandler } from "../functions/lib/handlers.mjs";
import { encryptPayload, signNonce, verifyState } from "../functions/lib/security.mjs";

const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 3072 });
const PUBLIC_PEM = publicKey.export({ type: "spki", format: "pem" });
const SECRET = "signing-secret";
const NONCE = "abcdefghijklmnopqrstuvwxyz012345";
const STATE = `${NONCE}.${signNonce(NONCE, SECRET)}`;
const ENV = {
  HUBSPOT_CLIENT_ID: "cid", HUBSPOT_CLIENT_SECRET: "csecret", OAUTH_REDIRECT_URI: "https://x/oauth/callback",
  STATE_SIGNING_SECRET: SECRET, TOKEN_PUBLIC_KEY_PEM: PUBLIC_PEM, TOKEN_FETCH_SECRET: "fetch-secret",
  ERROR_ROUTER_URL: "https://router.test/hook", FLOW_ID: "f1", FLOW_NAME: "Receiver", PLATFORM_NAME: "OTHER",
};

class Store {
  data = new Map();
  async get(k) { return this.data.get(k)?.data ?? null; }
  async getWithMetadata(k) { return this.data.get(k) ?? null; }
  async set(k, data, { metadata }) { this.data.set(k, { data, metadata }); }
  async delete(k) { this.data.delete(k); }
  async list({ prefix }) { return { blobs: [...this.data.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })) }; }
}

const hubspot = (calls = []) => async (url, init) => {
  calls.push({ url: String(url), init });
  if (String(url).includes("/oauth/v1/token")) return Response.json({ access_token: "at", refresh_token: "RT-SECRET-123", expires_in: 1800 });
  if (String(url).includes("/access-tokens/")) return Response.json({ hub_id: 99, scopes: ["a", "b"], user: "someone@example.com" });
  return new Response("{}", { status: 200 });
};
const req = (path, headers = {}) => new Request(`https://x${path}`, { headers });

test("verifyState accepts signed state and rejects forgeries", () => {
  assert.equal(verifyState(STATE, SECRET), NONCE);
  assert.equal(verifyState(`${NONCE}.${"0".repeat(32)}`, SECRET), null);
  assert.equal(verifyState("short.sig", SECRET), null);
  assert.equal(verifyState(null, SECRET), null);
});

test("callback stores only ciphertext that the private key decrypts", async () => {
  const store = new Store();
  const calls = [];
  const res = await createCallbackHandler({ env: ENV, store, fetchImpl: hubspot(calls), now: () => 1000 })(req(`/oauth/callback?code=thecode&state=${STATE}`));
  assert.equal(res.status, 200);
  const stored = store.data.get("portal/99");
  assert.ok(!stored.data.includes("RT-SECRET-123"));
  assert.deepEqual(stored.metadata, { stored_at: 1000, installed_at: 1000, last_used: null, scopes: ["a", "b"] });
  assert.deepEqual(JSON.parse(store.data.get(`state/${NONCE}`).data), { hub_id: "99" });
  const plain = crypto.privateDecrypt({ key: privateKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" }, Buffer.from(stored.data, "base64"));
  assert.equal(JSON.parse(plain).refresh_token, "RT-SECRET-123");
  assert.ok(!JSON.stringify(stored).includes("someone@example.com"));
  assert.equal(new URLSearchParams(calls[0].init.body).get("code"), "thecode");
});

test("callback rejects forged state, missing code, reuse; handles denial", async () => {
  const store = new Store();
  const handler = createCallbackHandler({ env: ENV, store, fetchImpl: hubspot() });
  assert.equal((await handler(req(`/oauth/callback?code=c&state=${NONCE}.${"f".repeat(32)}`))).status, 400);
  assert.equal((await handler(req(`/oauth/callback?state=${STATE}`))).status, 400);
  assert.equal((await handler(req("/oauth/callback?error=access_denied"))).status, 200);
  assert.equal(store.data.size, 0);
  await handler(req(`/oauth/callback?code=c&state=${STATE}`));
  assert.equal((await handler(req(`/oauth/callback?code=c&state=${STATE}`))).status, 409);
});

test("callback failure posts the Unified Error Payload without secrets, then rethrows", async () => {
  const posted = [];
  const fetchImpl = async (url, init) => {
    if (String(url).startsWith(ENV.ERROR_ROUTER_URL)) { posted.push(JSON.parse(init.body)); return new Response("", { status: 200 }); }
    return new Response("code thecode rejected", { status: 400 });
  };
  const handler = createCallbackHandler({ env: ENV, store: new Store(), fetchImpl });
  await assert.rejects(handler(req(`/oauth/callback?code=thecode&state=${STATE}`)));
  assert.equal(posted.length, 1);
  assert.deepEqual(Object.keys(posted[0]).sort(), ["correlation_id", "error_details", "flow_id", "flow_name", "process_name", "record_id", "source"]);
  assert.equal(posted[0].source, "OTHER");
  assert.ok(!JSON.stringify(posted[0]).includes("thecode"));
});

test("router retries 5xx once and never retries 4xx", async () => {
  const { postToErrorRouter } = await import("../functions/lib/error_router.mjs");
  let n = 0;
  await postToErrorRouter({ ERROR_ROUTER_URL: "u" }, {}, async () => { n++; return new Response("", { status: n === 1 ? 503 : 200 }); });
  assert.equal(n, 2);
  n = 0;
  await postToErrorRouter({ ERROR_ROUTER_URL: "u" }, {}, async () => { n++; return new Response("", { status: 400 }); });
  assert.equal(n, 1);
});

const AUTH = { authorization: "Bearer fetch-secret" };
const seeded = async (now = () => 2000) => {
  const store = new Store();
  await store.set("portal/99", "CIPHERTEXT", { metadata: { stored_at: 1000, installed_at: 1000, last_used: null, scopes: ["a"] } });
  await store.set(`state/${NONCE}`, JSON.stringify({ hub_id: "99" }), { metadata: { stored_at: 1000 } });
  return { store, handler: createTokenHandler({ env: ENV, store, now }) };
};

test("token handler requires the bearer secret and valid identifiers", async () => {
  const { handler } = await seeded();
  assert.equal((await handler(req("/api/token?portal=99"))).status, 401);
  assert.equal((await handler(req("/api/token?portal=99", { authorization: "Bearer wrong" }))).status, 401);
  assert.equal((await handler(req("/api/token?portal=../x", AUTH))).status, 400);
  assert.equal((await handler(req(`/api/token?state=${NONCE}.bad`, AUTH))).status, 400);
  assert.equal((await handler(req("/api/token?portal=12345", AUTH))).status, 404);
});

test("token reads are repeatable, resolve by state or portal, and record last_used", async () => {
  const { store, handler } = await seeded(() => 5000);
  for (const q of ["portal=99", `state=${STATE}`, "portal=99"]) {
    const res = await handler(req(`/api/token?${q}`, AUTH));
    assert.deepEqual(await res.json(), { hub_id: "99", ciphertext: "CIPHERTEXT", scopes: ["a"], installed_at: 1000 });
  }
  assert.equal(store.data.get("portal/99").metadata.last_used, 5000);
  assert.equal(store.data.get("portal/99").data, "CIPHERTEXT");
});

test("state lookup expires after 24h but the portal token does not", async () => {
  const { handler } = await seeded(() => 25 * 3600 * 1000);
  assert.equal((await handler(req(`/api/token?state=${STATE}`, AUTH))).status, 410);
  assert.equal((await handler(req("/api/token?portal=99", AUTH))).status, 200);
});

test("PUT replaces the ciphertext; DELETE offboards", async () => {
  const { store, handler } = await seeded();
  const put = (body) => handler(new Request("https://x/api/token?portal=99", { method: "PUT", headers: AUTH, body: JSON.stringify(body) }));
  assert.equal((await put({})).status, 400);
  assert.equal((await put({ ciphertext: "NEW" })).status, 200);
  assert.equal(store.data.get("portal/99").data, "NEW");
  assert.equal(store.data.get("portal/99").metadata.installed_at, 1000);
  const del = await handler(new Request("https://x/api/token?portal=99", { method: "DELETE", headers: AUTH }));
  assert.equal(del.status, 200);
  assert.equal((await handler(req("/api/token?portal=99", AUTH))).status, 404);
  assert.equal((await handler(new Request("https://x/api/token?portal=99", { method: "POST", headers: AUTH }))).status, 405);
});

test("clients list shows metadata without ciphertext", async () => {
  const { handler } = await seeded();
  const body = await (await handler(req("/api/clients", AUTH))).json();
  assert.deepEqual(body, { clients: [{ hub_id: "99", stored_at: 1000, installed_at: 1000, last_used: null, scopes: ["a"] }] });
  assert.ok(!JSON.stringify(body).includes("CIPHERTEXT"));
  assert.equal((await handler(req("/api/clients"))).status, 401);
});

test("reinstalling the same portal replaces the stored token", async () => {
  const store = new Store();
  const n2 = "zyxwvutsrqponmlkjihgfedcba987654";
  const state2 = `${n2}.${signNonce(n2, SECRET)}`;
  const handler = createCallbackHandler({ env: ENV, store, fetchImpl: hubspot(), now: () => 1 });
  await handler(req(`/oauth/callback?code=c&state=${STATE}`));
  const first = store.data.get("portal/99").data;
  await handler(req(`/oauth/callback?code=c&state=${state2}`));
  assert.notEqual(store.data.get("portal/99").data, first); // RSA-OAEP is randomized
  assert.equal([...store.data.keys()].filter((k) => k.startsWith("portal/")).length, 1);
});

test("encryptPayload refuses payloads larger than the key allows", () => {
  assert.throws(() => encryptPayload({ x: "a".repeat(400) }, PUBLIC_PEM), /exceeds/);
});

test("missing configuration fails fast", () => {
  assert.throws(() => createCallbackHandler({ env: { ...ENV, ERROR_ROUTER_URL: "" }, store: new Store() }), /ERROR_ROUTER_URL/);
  assert.throws(() => createTokenHandler({ env: {}, store: new Store() }), /Missing/);
});
