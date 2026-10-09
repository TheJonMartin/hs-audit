import crypto from "node:crypto";

const NONCE_PATTERN = /^[A-Za-z0-9_-]{16,64}$/;
const SIGNATURE_HEX_LENGTH = 32;
// RSA-OAEP with SHA-256 caps plaintext at (key bytes - 66); callers check this before encrypting.
const OAEP_SHA256_OVERHEAD_BYTES = 66;

/** Constant-time string comparison. */
export function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

/**
 * Compute the signature half of a state value.
 * @param {string} nonce
 * @param {string} secret
 * @returns {string} first 32 hex chars of HMAC-SHA256(nonce)
 */
export function signNonce(nonce, secret) {
  return crypto.createHmac("sha256", secret).update(nonce).digest("hex").slice(0, SIGNATURE_HEX_LENGTH);
}

/**
 * Validate a state of the form `<nonce>.<signature>` and return the nonce.
 * @param {string|null} state
 * @param {string} secret
 * @returns {string|null} the nonce, or null if the state is malformed or forged
 */
export function verifyState(state, secret) {
  if (typeof state !== "string") return null;
  const [nonce, signature, ...rest] = state.split(".");
  if (rest.length || !nonce || !signature || !NONCE_PATTERN.test(nonce)) return null;
  return safeEqual(signature, signNonce(nonce, secret)) ? nonce : null;
}

/**
 * Encrypt a JSON payload to the Process Pro public key (RSA-OAEP, SHA-256).
 * @param {object} payload
 * @param {string} publicKeyPem
 * @returns {string} base64 ciphertext
 * @throws {Error} if the payload is too large for the key
 */
export function encryptPayload(payload, publicKeyPem) {
  const key = crypto.createPublicKey(publicKeyPem);
  const maxBytes = key.asymmetricKeyDetails.modulusLength / 8 - OAEP_SHA256_OVERHEAD_BYTES;
  const data = Buffer.from(JSON.stringify(payload));
  if (data.length > maxBytes) {
    throw new Error(`Payload of ${data.length} bytes exceeds the ${maxBytes}-byte limit for this key.`);
  }
  return crypto
    .publicEncrypt({ key, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" }, data)
    .toString("base64");
}
