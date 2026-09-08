import { decode } from "./contract_validation.js";

export class ApiError extends Error {
  /** @param {"network" | "http" | "response"} kind @param {string} message
   * @param {number | null} [status] @param {unknown} [cause] */
  constructor(kind, message, status = null, cause = undefined) {
    super(message, { cause });
    this.kind = kind;
    this.status = status;
  }
}

/**
 * @template {keyof import('./contracts').ContractTypes} K
 * @param {string} path @param {K} contract
 * @param {{body?: object, signal?: AbortSignal}} [options]
 * @returns {Promise<import('./contracts').ContractTypes[K]>}
 */
export async function api(path, contract, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      signal: options.signal,
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      method: options.body ? "POST" : "GET",
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError("network", "The local server could not be reached.", null, error);
  }
  /** @type {unknown} */
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new ApiError("response", `The local server returned an invalid JSON response (HTTP ${response.status}).`, response.status, error);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new ApiError("response", "The local server returned an invalid response object.", response.status);
  }
  if (!response.ok) {
    throw new ApiError("http", "error" in payload && typeof payload.error === "string" ? payload.error : `Local server returned HTTP ${response.status}.`, response.status);
  }
  try { return decode(payload, contract); }
  catch (error) {
    throw new ApiError("response", error instanceof Error ? error.message : String(error), response.status, error);
  }
}
