// Local stand-in for Netlify: serves the real handlers over HTTP with an in-memory store and a
// fake HubSpot. For tests and for trying the flow without deploying. Not used in production.
import http from "node:http";
import { createCallbackHandler, createTokenHandler } from "./functions/lib/handlers.mjs";

class MemoryStore {
  data = new Map();
  async get(k) { return this.data.get(k)?.data ?? null; }
  async getWithMetadata(k) { return this.data.get(k) ?? null; }
  async set(k, data, { metadata } = {}) { this.data.set(k, { data, metadata }); }
  async delete(k) { this.data.delete(k); }
  async list({ prefix }) { return { blobs: [...this.data.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })) }; }
}

const FAKE_PORTAL = process.env.DEV_FAKE_HUB_ID || "4242";
const FAKE_REFRESH = process.env.DEV_FAKE_REFRESH_TOKEN || "dev-refresh-token";

// Fake HubSpot (token exchange) and fake error router; everything else is refused.
async function fakeFetch(url, init) {
  const target = String(url);
  if (target.endsWith("/oauth/2026-03/token")) {
    return Response.json({ access_token: "dev-access", refresh_token: FAKE_REFRESH, expires_in: 1800, hub_id: Number(FAKE_PORTAL), scopes: ["crm.objects.contacts.read"] });
  }
  if (target === process.env.ERROR_ROUTER_URL) {
    console.error("ERROR_ROUTER", init.body);
    return new Response("", { status: 200 });
  }
  return new Response("", { status: 500 });
}

const store = new MemoryStore();
const env = process.env;
const callback = createCallbackHandler({ env, store, fetchImpl: fakeFetch });
const tokens = createTokenHandler({ env, store, fetchImpl: fakeFetch });

const routes = [
  { path: "/oauth/callback", methods: ["GET"], handler: callback },
  { path: "/api/token", methods: ["GET", "PUT", "DELETE"], handler: tokens },
  { path: "/api/clients", methods: ["GET", "PUT", "DELETE"], handler: tokens },
];

const server = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const url = new URL(req.url, `http://${req.headers.host}`);
  const route = routes.find((r) => r.path === url.pathname);
  if (!route || !route.methods.includes(req.method)) {
    res.writeHead(404).end("not found");
    return;
  }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  const request = new Request(url, { method: req.method, headers: req.headers, body: hasBody ? Buffer.concat(chunks) : undefined });
  try {
    const response = await route.handler(request);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch (error) {
    res.writeHead(500).end(String(error.message));
  }
});

server.listen(Number(process.env.PORT || 0), "127.0.0.1", () => {
  console.log(`LISTENING ${server.address().port}`);
});
