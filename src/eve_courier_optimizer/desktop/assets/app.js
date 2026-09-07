import { $, $$, fmtNumber, fmtISK, fmtDuration, showNotice } from "./display.js";
import { api } from "./api.js";
import { PlanningForm } from "./planner_form.js";
import { renderProof, renderRoute, renderCommitments, renderRank } from "./route_view.js";

const state = {
  snapshot: null,
  plan: null,
  execution: null,
  busy: false,
  pendingArm: false,
  canArm: false,
  jobId: null,
};

const form = new PlanningForm();

async function waitForJob(job) {
  state.jobId = job.id;
  $("#cancel-job").classList.remove("hidden");
  $("#cancel-job").disabled = false;
  try {
    while (job.status === "running") {
      $("#busy-detail").textContent = job.progress;
      await new Promise((resolve) => window.setTimeout(resolve, 300));
      job = (await api(`/api/jobs/${job.id}`)).job;
    }
    if (job.status === "failed") throw new Error(job.error || "Background operation failed.");
    if (job.status === "cancelled") {
      showNotice("info", "Operation cancelled.", "The saved snapshot, plan and accepted commitments were preserved.");
      return null;
    }
    return job.result;
  } finally {
    state.jobId = null;
    $("#cancel-job").classList.add("hidden");
  }
}

async function runJob(operation, body) {
  const response = await api("/api/jobs", { body: { operation, input: body } });
  return waitForJob(response.job);
}

async function cancelJob() {
  if (!state.jobId) return;
  $("#cancel-job").disabled = true;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { body: {} });
  } catch (error) {
    $("#cancel-job").disabled = false;
    $("#busy-detail").textContent = error.message;
  }
}

function setBusy(active, title = "Working…", detail = "") {
  state.busy = active;
  $("#busy-title").textContent = title;
  $("#busy-detail").textContent = detail;
  $("#busy-layer").classList.toggle("hidden", !active);
  $("#workspace").inert = active;
  updateButtons();
}

async function withBusy(title, detail, action) {
  if (state.busy) return;
  setBusy(true, title, detail);
  const started = performance.now();
  const elapsed = () => {
    const seconds = (performance.now() - started) / 1000;
    $("#busy-elapsed").textContent = `Elapsed ${fmtDuration(seconds)} · you can return to this page while it runs`;
  };
  elapsed();
  const timer = setInterval(elapsed, 1000);
  try {
    return await action();
  } catch (error) {
    showNotice("error", "Action failed.", error instanceof Error ? error.message : String(error));
    return null;
  } finally {
    clearInterval(timer);
    $("#busy-elapsed").textContent = "";
    setBusy(false);
  }
}

function setWorkflow(stage) {
  const order = ["scan", "rank", "solve", "run"];
  const current = order.indexOf(stage);
  order.forEach((name, index) => {
    const node = $(`#step-${name}`);
    node.classList.toggle("done", index < current);
    node.classList.toggle("active", index === current);
  });
}

function updateButtons() {
  $("#start-execution").disabled = state.busy || !state.canArm;
  const hasSnapshot = Boolean(state.snapshot);
  const live = Boolean(state.execution);
  $("#scan-button").disabled = state.busy || live;
  $("#rank-button").disabled = !hasSnapshot || state.busy || live;
  $("#solve-button").disabled = !hasSnapshot || state.busy || live;
  const lockHint = "Planning is locked while an execution session is active. Resume the current route, refresh and replan, or end the execution session first.";
  for (const selector of ["#scan-button", "#rank-button", "#solve-button"]) {
    const button = $(selector);
    if (live) {
      button.title = lockHint;
      button.setAttribute("aria-describedby", "execution-lock-detail");
    } else {
      if (button.title === lockHint) button.removeAttribute("title");
      button.removeAttribute("aria-describedby");
    }
  }
  $("#download-plan").classList.toggle("disabled", !state.plan);
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  form.snapshot = snapshot;
  if (!snapshot) {
    $("#snapshot-pill").textContent = "No snapshot";
    $("#metric-observed").textContent = "--";
    $("#metric-scope").textContent = "Scan a region to establish scope";
    form.updateRiskStatus();
    setWorkflow("scan");
    updateButtons();
    return;
  }
  const regionNames = snapshot.region_names || [];
  const names = regionNames.length > 4
    ? `${regionNames.length} regions`
    : (regionNames.join(", ") || snapshot.region_ids.join(", "));
  updateSnapshotAge();
  $("#metric-observed").textContent = fmtNumber(snapshot.contracts);
  $("#metric-scope").textContent = `${names} · observed ${new Date(snapshot.fetched_at).toLocaleString()}`;
  form.updateRiskStatus();
  setWorkflow(state.plan ? "solve" : "rank");
  updateButtons();
}

function updateSnapshotAge() {
  if (!state.snapshot) return;
  const fetchedAt = Date.parse(state.snapshot.fetched_at);
  const ageSeconds = Number.isFinite(fetchedAt)
    ? Math.max(0, (Date.now() - fetchedAt) / 1000)
    : state.snapshot.age_seconds;
  $("#snapshot-pill").textContent = `${fmtNumber(state.snapshot.contracts)} couriers · ${fmtDuration(ageSeconds)} old`;
}

function renderPlan(plan) {
  state.plan = plan;
  renderProof(plan);
  renderRoute(plan, state.execution, state.pendingArm);
  if (!plan) {
    $("#metric-reward").textContent = "--";
    $("#metric-selected").textContent = "No route solved yet";
    $("#metric-duration").textContent = "--";
    $("#metric-route").textContent = "Modelled from your timing inputs";
    $("#execution-card").classList.add("hidden");
    updateButtons();
    return;
  }
  $("#metric-reward").textContent = fmtISK(plan.summary.total_reward_isk);
  const selected = plan.summary.selected_contract_ids?.length || 0;
  $("#metric-selected").textContent = `${selected} optional contract${selected === 1 ? "" : "s"} selected`;
  $("#metric-duration").textContent = fmtDuration(plan.summary.finish_seconds);
  const contractEvents = plan.route?.length || 0;
  const travelLegs = plan.travel_legs?.length || 0;
  $("#metric-route").textContent = `${contractEvents} contract events · ${travelLegs} travel legs`;
  $("#execution-card").classList.remove("hidden");
  setWorkflow(state.execution ? "run" : "solve");
  updateButtons();
}

function renderExecutionLock(execution) {
  const banner = $("#execution-lock-banner");
  const topPill = $("#execution-top-pill");
  const endButton = $("#end-execution-banner");
  if (!execution) {
    banner.classList.add("hidden");
    topPill.classList.add("hidden");
    endButton.classList.add("hidden");
    return;
  }

  const activeCount = Number(execution.active_count || 0);
  const safeToEnd = Boolean(execution.can_end_safely);
  const currentSystem = execution.current_system_name || execution.current_system_id || "unknown system";
  const deadline = execution.session_deadline ? new Date(execution.session_deadline) : null;
  const expired = deadline instanceof Date && Number.isFinite(deadline.getTime()) && deadline.getTime() < Date.now();

  banner.classList.remove("hidden");
  topPill.classList.remove("hidden");
  endButton.classList.toggle("hidden", !safeToEnd);
  $("#execution-top-pill-text").textContent = safeToEnd
    ? "Execution open · safe to end"
    : `Live route · ${activeCount} committed`;

  if (safeToEnd) {
    $("#execution-lock-title").textContent = "The previous execution session is still open";
    $("#execution-lock-detail").textContent = "No accepted courier commitments remain. End execution to unlock a fresh Scan, Rank and Solve, or resume the route if you still need its waypoints.";
  } else if (expired) {
    $("#execution-lock-title").textContent = "The saved route is past its planning horizon";
    $("#execution-lock-detail").textContent = `${activeCount} accepted courier commitment${activeCount === 1 ? " is" : "s are"} still protected at ${currentSystem}. The optimizer will not forget them automatically; record real progress or deliberately reset the live session.`;
  } else {
    $("#execution-lock-title").textContent = "A courier route is still in progress";
    $("#execution-lock-detail").textContent = `${activeCount} accepted courier commitment${activeCount === 1 ? " is" : "s are"} protected at ${currentSystem}. Scan, Rank and Solve stay locked so a restart cannot silently discard them; use Refresh market & replan for new opportunities.`;
  }
}

async function extendHorizon() {
  const result = await withBusy("Extending planning horizon…", "Preserving every contract deadline.",
    () => api("/api/execution/extend", { body: { minutes: $("#extend-minutes").value } }));
  if (!result) return;
  state.pendingArm = false;
  state.canArm = false;
  renderPlan(null);
  renderExecution(result.execution);
  showNotice("success", "Planning horizon extended.", "Accepted contracts and their deadlines are unchanged. Replan to compute a new route.");
}

function renderExecution(execution) {
  state.execution = execution;
  renderCommitments(execution);
  const card = $("#execution-card");
  const commit = $("#commit-box");
  const liveTools = $("#live-tools");
  if (!execution) {
    $("#execution-state-pill").textContent = "Plan ready";
    $("#exec-system").textContent = "--";
    $("#exec-active").textContent = "0";
    $("#exec-cargo").textContent = "0 m³";
    $("#exec-collateral").textContent = "0 ISK";
    commit.classList.toggle("hidden", !state.plan);
    liveTools.classList.add("hidden");
    if (!state.plan) card.classList.add("hidden");
  } else {
    card.classList.remove("hidden");
    commit.classList.toggle("hidden", !state.pendingArm);
    liveTools.classList.remove("hidden");
    $("#execution-state-pill").textContent = state.pendingArm
      ? "Replan review"
      : execution.can_end_safely
        ? "No accepted commitments"
        : "Execution active";
    $("#exec-system").textContent = execution.current_system_name || execution.current_system_id;
    $("#exec-active").textContent = fmtNumber(execution.active_count);
    $("#exec-cargo").textContent = `${fmtNumber(execution.cargo_in_use_m3, 3)} m³`;
    $("#exec-collateral").textContent = fmtISK(execution.collateral_locked_isk);
    const guidance = $("#execution-guidance");
    const resetButton = $("#reset-execution");
    if (state.pendingArm) {
      guidance.textContent = "This revised locked-mode plan is only a proposal until you apply it. Existing accepted commitments remain live and can still be recorded. If a proposed new contract is unavailable, refresh and replan again instead of arming it.";
    } else if (execution.can_end_safely) {
      guidance.textContent = "No accepted courier commitments remain. You may keep following or replanning this trip, or end execution to unlock a completely new scan and plan.";
    } else {
      guidance.textContent = "Accepted courier commitments are protected during replanning and cannot be silently dropped. Record real pickups and deliveries as they happen.";
    }
    if (execution.can_end_safely) {
      resetButton.textContent = "End execution & start new plan";
      resetButton.classList.remove("danger");
    } else {
      resetButton.textContent = "Reset live session (advanced)";
      resetButton.classList.add("danger");
    }
    setWorkflow("run");
  }
  renderExecutionLock(execution);
  const locked = (state.plan?.model?.collateral_mode || state.execution?.collateral_mode) === "locked";
  $("#locked-confirm-row").classList.toggle("hidden", !locked);
  renderRoute(state.plan, state.execution, state.pendingArm);
  updateButtons();
}

async function loadStatus() {
  try {
    const payload = await api("/api/status");
    if (payload.job?.status === "running") {
      await withBusy("Resuming background operation…", payload.job.progress,
        () => waitForJob(payload.job));
      return loadStatus();
    }
    state.canArm = Boolean(payload.plan_armable);
    state.pendingArm = state.canArm && Boolean(payload.execution)
      && payload.plan?.model?.collateral_mode === "locked"
      && (payload.plan?.summary?.selected_contract_ids?.length || 0) > 0;
    $("#sde-pill").textContent = `SDE ${payload.sde.build_number} · ${fmtNumber(payload.sde.systems)} systems`;
    renderSnapshot(payload.snapshot);
    form.hydratePlannerFromPlan(payload.plan);
    renderPlan(payload.plan);
    renderExecution(payload.execution);
    if (payload.job?.operation === "rank" && payload.job.status === "completed") {
      renderRank(payload.job.result);
    }
    if (payload.execution) {
      showNotice("warning", "Live execution restored.", "This route survived the restart. Planning controls remain locked until you end the session; use the persistent banner above to resume it.");
    } else if (payload.snapshot) {
      showNotice("info", "Snapshot restored.", "You can inspect or solve it immediately, or scan again for a fresh market observation.");
    }
  } catch {
    showNotice(
      "warning",
      "Local backend not connected.",
      "This interface is normally opened by `eve-courier web`. Start that command and refresh this page.",
    );
    $("#sde-pill").textContent = "Backend offline";
  }
}

async function scan() {
  if (form.regionScope === "selected" && !form.selectedRegions.length) {
    showNotice("error", "Region required.", "Search and add at least one region, or choose a region preset.");
    return;
  }
  const body = form.regionScope === "selected"
    ? { region_scope: "selected", regions: form.selectedRegions.map((item) => item.id || item.name) }
    : { region_scope: form.regionScope };
  if (form.regionScope === "security" || form.regionScope === "empire") {
    body.security_bands = form.plannerPayload().security_bands;
  }
  body.include_threat_intel = $("#gank-awareness").checked;
  body.threat_window_hours = $("#threat-window-hours").value;
  body.threat_gate_radius_km = $("#threat-gate-radius-km").value;
  if (body.include_threat_intel) {
    const planning = form.plannerPayload();
    body.threat_scope_to_plan = true;
    body.start = planning.start;
    body.duration_hours = planning.duration_hours;
    body.duration_minutes = planning.duration_minutes;
    body.security_bands = planning.security_bands;
    body.seconds_per_jump = planning.seconds_per_jump;
  }
  const result = await withBusy(
    "Scanning public courier contracts…",
    form.regionScope === "all"
      ? "Scanning all contract regions with bounded ESI concurrency; gate intel is limited to the proof-safe reachable route envelope."
      : form.regionScope === "security"
        ? "Skipping SDE regions that contain no system in the selected security bands; mixed regions are retained."
        : "Using bounded ESI concurrency and cache/rate-limit handling; gate intel covers every region this configured route could traverse.",
    () => runJob("scan", body),
  );
  if (!result) return;
  state.canArm = false;
  renderSnapshot(result.snapshot);
  renderPlan(null);
  renderRank(null);
  const riskRequested = $("#gank-awareness").checked;
  const riskReady = Boolean(result.snapshot.threat_intel_fetched_at);
  const incompleteRegions = result.snapshot.threat_incomplete_region_ids?.length || 0;
  const riskComplete = riskReady && incompleteRegions === 0;
  const scope = result.snapshot.region_names.length > 4
    ? `${result.snapshot.region_names.length} regions`
    : result.snapshot.region_names.join(", ");
  showNotice(
    !riskRequested || riskComplete ? "success" : "warning",
    "Snapshot captured.",
    `${fmtNumber(result.snapshot.contracts)} public couriers observed across ${scope}. ${riskReady ? `${fmtNumber(result.snapshot.gate_threat_events)} gate-relevant zKill events captured${incompleteRegions ? `; ${incompleteRegions} region observations are incomplete` : ""}.` : riskRequested ? "Gate intel is unavailable; normal solving still works." : "Gate intel was not requested."}`,
  );
}

async function rank() {
  const result = await withBusy(
    "Ranking feasible opportunities…",
    "Applying endpoint, security, danger-policy, capacity, collateral, expiry and horizon filters.",
    () => runJob("rank", form.plannerPayload()),
  );
  if (!result) return;
  renderRank(result);
  setWorkflow("rank");
  const truncated = !result.scope.scope_untruncated;
  showNotice(
    truncated ? "warning" : "success",
    `${fmtNumber(result.scope.eligible_contracts)} contracts are individually eligible.`,
    truncated ? "A heuristic candidate cap is active, so any subsequent optimality proof has a truncated scope." : "No heuristic candidate truncation is active.",
  );
}

async function solve() {
  const cap = $("#max-candidates").value.trim();
  const result = await withBusy(
    "Optimizing route & proving reward…",
    cap ? "Exact inside the retained candidate set. The certificate will mark the global scope as truncated." : "No candidate cap: the solver is working over every eligible contract retained by safe reductions.",
    () => runJob("solve", form.plannerPayload()),
  );
  if (!result) return;
  state.pendingArm = false;
  state.canArm = Boolean(result.plan.certificate.feasibility_verified);
  renderPlan(result.plan);
  renderExecution(null);
  const cert = result.plan.certificate;
  const global = cert.status === "proven_optimal" && cert.scope_untruncated;
  showNotice(
    global ? "success" : "warning",
    global ? "Global optimum proven for this model and snapshot." : "Route solved; read the certificate scope.",
    global ? `Reward ${fmtISK(result.plan.summary.total_reward_isk)} matches the solver's best bound.` : cert.claim,
  );
}

async function startExecution() {
  const locked = (state.plan?.model?.collateral_mode || state.execution?.collateral_mode) === "locked";
  const selected = state.plan?.summary?.selected_contract_ids?.length || 0;
  if (locked && selected > 0 && !$("#locked-confirm").checked) {
    showNotice("warning", "Acceptance confirmation required.", "Check the box only after accepting every selected contract in EVE.");
    return;
  }
  const result = await withBusy(
    "Arming execution state…",
    "Persisting accepted commitments so replanning cannot silently drop them.",
    () => api("/api/execution/start", { body: { confirm_locked_acceptance: locked && selected > 0 && $("#locked-confirm").checked } }),
  );
  if (!result) return;
  state.pendingArm = false;
  state.canArm = false;
  renderExecution(result.execution);
  showNotice("success", "Execution session armed.", "Use the route-table buttons to record real pickups and deliveries.");
}

async function recordAction(action, contractId) {
  if (!action || !contractId) return;
  const invalidatesReview = state.pendingArm;
  const result = await withBusy(
    `Recording ${action}…`,
    `Contract #${contractId}; the transition is validated before persistent state is changed.`,
    () => api("/api/action", { body: { action, contract_id: contractId, at: "now" } }),
  );
  if (!result) return;
  state.canArm = false;
  if (invalidatesReview) state.pendingArm = false;
  renderExecution(result.execution);
  if (invalidatesReview) {
    showNotice(
      "warning",
      `${action === "pickup" ? "Pickup" : "Delivery"} recorded; proposal expired.`,
      `Contract #${contractId} changed the live state, so the unarmed revised plan is no longer armable. Refresh and replan again from the new facts.`,
    );
  } else {
    showNotice("success", `${action === "pickup" ? "Pickup" : "Delivery"} recorded.`, `Contract #${contractId} is reflected in the live execution state.`);
  }
}

async function recordRouteSystem(systemId, systemName) {
  const invalidatesReview = state.pendingArm;
  const result = await withBusy(
    "Recording route progress…",
    `${systemName} is being marked as reached in the persisted execution state.`,
    () => api("/api/action", { body: { action: "route_system", system_id: systemId, at: "now" } }),
  );
  if (!result) return;
  state.canArm = false;
  if (invalidatesReview) state.pendingArm = false;
  renderExecution(result.execution);
  showNotice(
    invalidatesReview ? "warning" : "success",
    invalidatesReview ? "Route progress recorded; proposal expired." : "Route progress recorded.",
    invalidatesReview
      ? `${systemName} is now satisfied, and the unarmed revised plan must be recomputed from this new state.`
      : `${systemName} is now satisfied for this trip.`,
  );
}

async function replan() {
  const result = await withBusy(
    "Refreshing market & replanning…",
    "Accepted contracts remain mandatory; fresh public opportunities may be added around them.",
    () => runJob("replan", { ...form.plannerPayload(), refresh: true }),
  );
  if (!result) return;
  state.canArm = Boolean(result.plan.certificate?.feasibility_verified);
  state.pendingArm = state.canArm && result.execution?.collateral_mode === "locked"
    && (result.plan.summary?.selected_contract_ids?.length || 0) > 0;
  renderSnapshot(result.snapshot);
  form.hydratePlannerFromPlan(result.plan);
  renderPlan(result.plan);
  renderExecution(result.execution);
  $("#locked-confirm").checked = false;
  if (result.plan.certificate?.status === "proven_infeasible") {
    state.pendingArm = false;
    renderExecution(result.execution);
    if ((result.execution?.active_count || 0) > 0) {
      showNotice(
        "error",
        "Remaining trip is proven infeasible.",
        "The accepted commitments cannot all fit the remaining time and route policy. The execution session is still usable: record actual progress and replan again, or reset only if you deliberately want to stop preserving those commitments.",
      );
    } else {
      showNotice(
        "warning",
        "This trip is proven infeasible.",
        "No accepted courier commitments remain, so you can safely end execution and start a fresh plan with different route or time constraints.",
      );
    }
    return;
  }
  if (state.pendingArm) {
    $("#start-execution").textContent = "Apply revised plan to execution";
    showNotice("warning", "Replan is ready but not armed yet.", "Review the new certificate and route. In locked mode, accept any newly selected contracts in EVE before applying the revised plan.");
  } else if (state.canArm) {
    showNotice("success", "Replan ready.", "Existing accepted shipments remain mandatory; future optional jobs become commitments only when you record their pickups.");
  }
}

async function resetExecution() {
  const canEndSafely = Boolean(state.execution?.can_end_safely);
  const question = canEndSafely
    ? "End this execution session and unlock a new scan and plan?"
    : "Reset the persisted live execution session? Accepted courier commitments are still recorded. Continue only if you deliberately want the optimizer to stop preserving them.";
  if (!window.confirm(question)) return;
  const result = await withBusy(
    canEndSafely ? "Ending execution session…" : "Resetting execution session…",
    "The snapshot and last plan remain available; the persisted live-state file is removed.",
    () => api("/api/execution/reset", { body: {} }),
  );
  if (!result) return;
  state.pendingArm = false;
  state.canArm = false;
  renderExecution(null);
  $("#start-execution").textContent = "Arm this plan for execution";
  if (canEndSafely) {
    showNotice("success", "Execution ended.", "Planning is unlocked. Scan fresh market data or solve again from the current snapshot.");
  } else {
    showNotice("warning", "Live session reset.", "The optimizer no longer treats previously recorded commitments as mandatory. Verify your in-game contracts before solving again.");
  }
}

function wireEvents() {
  $("#cancel-job").addEventListener("click", cancelJob);
  $("#extend-horizon").addEventListener("click", extendHorizon);
  $("#scan-button").addEventListener("click", scan);
  $("#rank-button").addEventListener("click", rank);
  $("#solve-button").addEventListener("click", solve);
  $("#start-execution").addEventListener("click", startExecution);
  $("#replan-button").addEventListener("click", replan);
  $("#reset-execution").addEventListener("click", resetExecution);
  $("#resume-execution").addEventListener("click", () => {
    $("#execution-card").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#end-execution-banner").addEventListener("click", resetExecution);
  $("#collateral-mode").addEventListener("change", () => renderExecution(state.execution));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button.row-action");
    if (!button || button.disabled) return;
    if (button.dataset.contractId) recordAction(button.dataset.action, button.dataset.contractId);
    else if (button.dataset.systemId) recordRouteSystem(button.dataset.systemId, button.dataset.systemName);
  });
}

wireEvents();
loadStatus();
window.setInterval(updateSnapshotAge, 1000);
