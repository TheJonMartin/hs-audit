import { getStore } from "@netlify/blobs";
import { createCallbackHandler } from "./lib/handlers.mjs";

export const config = { path: "/oauth/callback", method: "GET" };

export default async (request) =>
  createCallbackHandler({ env: process.env, store: getStore("oauth-tokens") })(request);
