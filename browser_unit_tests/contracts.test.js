import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { decode } from "../src/eve_courier_optimizer/web/assets/contract_validation.js";
import { api, ApiError } from "../src/eve_courier_optimizer/web/assets/api.js";

const running = { id: "review-job", operation: "solve", status: "running", progress: "Solving", elapsed_seconds: 0 };
// Complete response captured from a synthetic Alpha-to-Beta solve; no schema-generated defaults.
const planResponse = JSON.parse(readFileSync(new URL("./fixtures/route-response.json", import.meta.url), "utf8"));

test("job outcomes require the fields belonging to their state", () => {
  assert.deepEqual(decode({ job: running }, "JobEnvelope"), { job: running });
  assert.throws(() => decode({ job: { ...running, status: "completed" } }, "JobEnvelope"), TypeError);
  assert.throws(() => decode({ job: { ...running, status: "failed", error: null } }, "JobEnvelope"), TypeError);
  assert.equal(decode({ job: { ...running, status: "failed", error: "Disk full" } }, "JobEnvelope").job.status, "failed");
});

test("a plan response rejects an invalid nested system ID", () => {
  assert.deepEqual(decode(planResponse, "PlanResponse"), planResponse);
  const invalid = structuredClone(planResponse);
  invalid.plan.model.start_system_id = [];
  assert.throws(() => decode(invalid, "PlanResponse"), {
    name: "TypeError", message: "PlanResponse.plan.model.start_system_id has an invalid primitive type.",
  });
});

test("an unconfigured radius is null; a missing policy field is rejected", () => {
  assert.equal(decode(planResponse, "PlanResponse").plan.model.threat_gate_radius_m, null);
  const invalid = structuredClone(planResponse);
  delete invalid.plan.model.threat_gate_radius_m;
  assert.throws(() => decode(invalid, "PlanResponse"), {
    name: "TypeError", message: "PlanResponse.plan.model.threat_gate_radius_m is required.",
  });
});

test("suggestions reject booleans, unsafe IDs and malformed array members", () => {
  for (const id of [true, "42", Number.MAX_SAFE_INTEGER + 1, NaN]) {
    assert.throws(() => decode({ items: [{ id, name: "Alpha" }] }, "Suggestions"), TypeError);
  }
  assert.throws(() => decode({ items: [null] }, "Suggestions"), TypeError);
  assert.deepEqual(decode({ items: [{ id: 42, name: "Alpha", security_status: 0, added_field: true }] }, "Suggestions").items[0].security_status, 0);
});

test("missing fields and null have different meanings", () => {
  assert.deepEqual(decode({ revision: 1, proposal_id: null, execution: null }, "ExecutionResponse").execution, null);
  assert.throws(() => decode({ revision: 1, execution: null }, "ExecutionResponse"), TypeError);
  assert.throws(() => decode({ revision: true, proposal_id: null, execution: null }, "ExecutionResponse"), TypeError);
});

test("fetch failures, HTTP failures and malformed success bodies stay distinguishable", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new TypeError("connection refused"); });
  await assert.rejects(api("/api/jobs/1", "JobEnvelope"), e => e instanceof ApiError && e.kind === "network");
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "disk full" }), { status: 500 });
  await assert.rejects(api("/api/jobs/1", "JobEnvelope"), e => e instanceof ApiError && e.kind === "http" && e.status === 500);
  globalThis.fetch = async () => new Response(JSON.stringify({ job: { ...running, status: "completed" } }));
  await assert.rejects(api("/api/jobs/1", "JobEnvelope"), e => e instanceof ApiError && e.kind === "response");
});
