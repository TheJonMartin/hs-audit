import { getStore } from "@netlify/blobs";
import { createTokenHandler } from "./lib/handlers.mjs";

export const config = { path: ["/api/token", "/api/clients"] };

export default async (request) =>
  createTokenHandler({ env: process.env, store: getStore("oauth-tokens") })(request);
