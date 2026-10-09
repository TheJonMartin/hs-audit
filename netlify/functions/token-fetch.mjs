import { getStore } from "@netlify/blobs";
import { createFetchHandler } from "./lib/handlers.mjs";

export const config = { path: "/api/token", method: "GET" };

export default async (request) =>
  createFetchHandler({ env: process.env, store: getStore("oauth-tokens") })(request);
