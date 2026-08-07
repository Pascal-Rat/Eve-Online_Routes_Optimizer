"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  snapshot: null,
  plan: null,
  execution: null,
  rank: null,
  busy: false,
  pendingArm: false,
  regionScope: "security",
  selectedRegions: [],
  avoidedSystems: [],
  requiredSystems: [],
};

const defaults = {
  start: "Jita",
  "cargo-m3": "62500",
  "collateral-isk": "1",
  "collateral-unit": "b",
  "duration-hours": "3",
  "duration-minutes": "0",
  "collateral-mode": "locked",
  "max-simultaneous-contracts": "",
  "threat-min-events": "1",
  "threat-window-hours": "2",
  "threat-gate-radius-km": "250",
  "seconds-per-jump": "60",
  "service-seconds": "30",
  "time-limit": "60",
  workers: "4",
  "max-candidates": "",
};

function fmtNumber(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(number);
}

function fmtISK(value, compact = true) {
  if (value === null || value === undefined) return "--";
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} ISK`;
  if (compact) {
    const abs = Math.abs(n);
    if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B ISK`;
    if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M ISK`;
    if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K ISK`;
  }
  return `${fmtNumber(n, 2)} ISK`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "--";
  const total = Math.max(0, Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function plannerPayload() {
  const cap = $("#max-candidates").value.trim();
  const simultaneousCap = $("#max-simultaneous-contracts").value.trim();
  const startId = $("#start").dataset.systemId;
  const finishInput = $("#finish-system");
  const finish = $("#return-to-start").checked
    ? null
    : (finishInput.dataset.systemId || finishInput.value.trim() || null);
  return {
    start: startId || $("#start").value.trim(),
    cargo_m3: $("#cargo-m3").value,
    collateral_isk: $("#collateral-isk").value,
    collateral_unit: $("#collateral-unit").value,
    duration_hours: $("#duration-hours").value,
    duration_minutes: $("#duration-minutes").value,
    security_bands: $$('input[name="security-band"]:checked').map((input) => input.value),
    collateral_mode: $("#collateral-mode").value,
    avoid_systems: state.avoidedSystems.map((item) => item.id || item.name),
    required_systems: state.requiredSystems.map((item) => item.id || item.name),
    return_to_start: $("#return-to-start").checked,
    finish_system: finish,
    max_simultaneous_contracts: simultaneousCap === "" ? null : simultaneousCap,
    gank_awareness: $("#gank-awareness").checked,
    threat_categories: $$('input[name="threat-category"]:checked').map((input) => input.value),
    threat_min_events: $("#threat-min-events").value,
    seconds_per_jump: $("#seconds-per-jump").value,
    service_seconds: $("#service-seconds").value,
    time_limit: $("#time-limit").value,
    workers: $("#workers").value,
    max_candidates: cap === "" ? null : cap,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    method: options.body ? "POST" : "GET",
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`Local server returned HTTP ${response.status}.`);
  }
  if (!response.ok) throw new Error(payload.error || `Local server returned HTTP ${response.status}.`);
  return payload;
}

function setBusy(active, title = "Working…", detail = "") {
  state.busy = active;
  $("#busy-title").textContent = title;
  $("#busy-detail").textContent = detail;
  $("#busy-layer").classList.toggle("hidden", !active);
  $$(`button`).forEach((button) => {
    if (button.id !== "reset-defaults") button.dataset.wasDisabled = String(button.disabled);
  });
}

async function withBusy(title, detail, action) {
  if (state.busy) return;
  setBusy(true, title, detail);
  const started = performance.now();
  const elapsed = () => {
    const seconds = (performance.now() - started) / 1000;
    $("#busy-elapsed").textContent = `Elapsed ${fmtDuration(seconds)} · leave this tab open`;
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

function showNotice(kind, title, detail) {
  const notice = $("#notice");
  notice.className = `notice ${kind}`;
  notice.querySelector(".notice-icon").textContent = kind === "error" ? "!" : kind === "success" ? "✓" : kind === "warning" ? "!" : "i";
  notice.querySelector("div").replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = `${title} `;
  notice.querySelector("div").append(strong, document.createTextNode(detail));
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
  if (!snapshot) {
    $("#snapshot-pill").textContent = "No snapshot";
    $("#metric-observed").textContent = "--";
    $("#metric-scope").textContent = "Scan a region to establish scope";
    updateRiskStatus();
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
  updateRiskStatus();
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

function setCheck(selector, passed, yes, no) {
  const node = $(selector);
  node.textContent = `${passed ? "✓" : "△"} ${passed ? yes : no}`;
  node.classList.toggle("good", Boolean(passed));
  node.classList.toggle("warn", !passed);
}

function renderProof(plan) {
  const empty = $("#proof-empty");
  const content = $("#proof-content");
  const badge = $("#proof-badge");
  if (!plan?.certificate) {
    empty.classList.remove("hidden");
    content.classList.add("hidden");
    badge.className = "proof-badge neutral";
    badge.textContent = "Not solved";
    return;
  }
  empty.classList.add("hidden");
  content.classList.remove("hidden");
  const cert = plan.certificate;
  const proven = cert.status === "proven_optimal";
  const global = proven && cert.scope_untruncated;
  if (global) {
    badge.className = "proof-badge success";
    badge.textContent = "Proven global optimal*";
  } else if (proven) {
    badge.className = "proof-badge warning";
    badge.textContent = "Optimal · truncated scope";
  } else if (cert.status === "feasible_not_proven") {
    badge.className = "proof-badge warning";
    badge.textContent = "Feasible · proof open";
  } else {
    badge.className = "proof-badge error";
    badge.textContent = cert.status.replaceAll("_", " ");
  }
  $("#proof-objective").textContent = fmtISK(cert.objective_isk);
  $("#proof-bound").textContent = fmtISK(cert.best_bound_isk);
  $("#proof-gap").textContent = cert.relative_gap === null ? "--" : `${(Number(cert.relative_gap) * 100).toFixed(4)}%`;
  $("#proof-time").textContent = `${Number(cert.wall_time_seconds).toFixed(2)} s`;
  const eligible = plan.scope?.eligible_contracts;
  const observed = plan.scope?.public_couriers_seen;
  setCheck("#proof-scope-check", cert.scope_untruncated, "Full eligible scope retained", "Heuristic candidate truncation applied");
  if (eligible !== undefined && observed !== undefined) {
    $("#proof-scope-check").textContent += ` · ${fmtNumber(eligible)} eligible / ${fmtNumber(observed)} observed`;
  }
  setCheck("#proof-feasible-check", cert.feasibility_verified, "Independent feasibility simulation passed", "Feasibility verification unavailable");
  const refKnown = Boolean(cert.independent_reference_verified);
  setCheck("#proof-reference-check", refKnown, "Independent small-case reference agreed", "Reference cross-check not applicable at this size");
  const strengthening = cert.bound_strengthening || {};
  const relaxationBound = strengthening.system_relaxation_bound_isk;
  const relaxationStatus = strengthening.system_relaxation_status;
  if (relaxationStatus) {
    const strengthened = relaxationBound !== null && relaxationBound !== undefined;
    const relaxationTime = Number(strengthening.system_relaxation_wall_time_seconds || 0).toFixed(2);
    let details = strengthened
      ? `System relaxation ${relaxationStatus.toLowerCase()}: ${fmtISK(relaxationBound)} ceiling across ${fmtNumber(strengthening.system_relaxation_systems || 0)} endpoint systems in ${relaxationTime} s; ${fmtNumber(strengthening.incompatibility_pairs || 0)} pair conflicts and ${fmtNumber(strengthening.incompatibility_cliques || 0)} clique cuts`
      : `System relaxation ${relaxationStatus.toLowerCase()}; no additional ceiling was available before its prepass limit`;
    if (strengthening.decomposition_status) {
      const decompositionStatus = String(strengthening.decomposition_status).replaceAll("_", " ");
      const iterations = fmtNumber(strengthening.decomposition_iterations || 0);
      const learnedCuts = fmtNumber(strengthening.decomposition_learned_cuts || 0);
      const exactTime = Number(strengthening.decomposition_subproblem_wall_time_seconds || 0).toFixed(2);
      details += `; master-guided ${decompositionStatus}, ${iterations} iteration(s), ${learnedCuts} learned core cut(s), ${exactTime} s exact-subproblem time`;
      if (strengthening.decomposition_proof_closed) {
        details += "; reward ceiling matched an independently verified exact route";
      }
    }
    setCheck("#proof-bound-strength-check", strengthened, details, details);
  } else {
    setCheck("#proof-bound-strength-check", true, "Bound prepass not needed for this small candidate set", "Bound prepass not needed for this small candidate set");
  }
  $("#proof-claim").textContent = cert.claim || "";
}

function executionMap() {
  const map = new Map();
  for (const shipment of state.execution?.active_shipments || []) {
    map.set(Number(shipment.contract.contract_id), shipment);
  }
  return map;
}

function makeCell(text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderRoute(plan) {
  const route = plan?.route || [];
  $("#route-empty").classList.toggle("hidden", route.length > 0);
  $("#route-wrap").classList.toggle("hidden", route.length === 0);
  const tbody = $("#route-body");
  tbody.replaceChildren();
  const active = executionMap();
  const completed = new Set(
    (state.execution?.completed_contract_ids || []).map((contractId) => Number(contractId)),
  );
  const lockedExecution = state.execution?.collateral_mode === "locked";
  const live = Boolean(state.execution);
  $$(".execution-column").forEach((cell) => cell.classList.toggle("hidden", !live));

  for (const step of route) {
    const tr = document.createElement("tr");
    tr.append(makeCell(String(step.sequence).padStart(2, "0")));

    const actionTd = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `action-tag ${step.action}`;
    tag.textContent = step.action;
    actionTd.append(tag);
    if (step.mandatory) {
      const mandatory = document.createElement("span");
      mandatory.className = "mandatory-tag";
      mandatory.textContent = "MANDATORY";
      actionTd.append(mandatory);
    }
    tr.append(actionTd);

    const contractTd = document.createElement("td");
    contractTd.className = "contract-cell";
    const contractTitle = document.createElement("strong");
    contractTitle.textContent = step.title || `Contract ${step.contract_id}`;
    contractTitle.title = step.title || `Contract ${step.contract_id}`;
    const contractId = document.createElement("small");
    contractId.textContent = `#${step.contract_id}`;
    contractTd.append(contractTitle, contractId);
    tr.append(contractTd);

    tr.append(makeCell(step.system_name || String(step.system_id)));
    tr.append(makeCell(fmtNumber(step.jump_count)));
    tr.append(makeCell(`${fmtNumber(step.cargo_after_m3, 3)} m³`));
    tr.append(makeCell(fmtISK(step.collateral_after_isk)));
    tr.append(makeCell(fmtISK(step.cumulative_reward_isk), "reward-cell"));

    const liveTd = document.createElement("td");
    liveTd.className = `execution-column${live ? "" : " hidden"}`;
    if (live) {
      const shipment = active.get(Number(step.contract_id));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "row-action";
      button.dataset.action = step.action;
      button.dataset.contractId = String(step.contract_id);
      if (completed.has(Number(step.contract_id))) {
        button.disabled = true;
        button.textContent = "Completed ✓";
      } else if (state.pendingArm && !shipment) {
        button.disabled = true;
        button.textContent = "Not armed";
      } else if (lockedExecution && !shipment) {
        button.disabled = true;
        button.textContent = "Not accepted";
      } else if (step.action === "delivery" && (!shipment || !shipment.picked)) {
        button.disabled = true;
        button.textContent = "Await pickup";
      } else if (step.action === "pickup" && shipment?.picked) {
        button.disabled = true;
        button.textContent = "Picked up ✓";
      } else {
        button.textContent = step.action === "pickup" ? "Record pickup" : "Record delivery";
      }
      liveTd.append(button);
    }
    tr.append(liveTd);
    tbody.append(tr);
  }

  tbody.querySelectorAll(".row-action").forEach((button) => {
    button.addEventListener("click", () => recordAction(button.dataset.action, button.dataset.contractId));
  });
  renderPilotRoute(plan);
}

function renderPilotRoute(plan) {
  const travelLegs = plan?.travel_legs || [];
  const panel = $("#pilot-route");
  const legs = $("#pilot-legs");
  panel.classList.toggle("hidden", travelLegs.length === 0);
  legs.replaceChildren();
  if (!travelLegs.length) return;

  const threatAware = (plan.model?.threat_categories?.length || 0) > 0;
  $("#route-policy-badge").textContent = threatAware ? "Threat-filtered transit" : "Security-filtered transit";
  $("#pilot-route-note").textContent = threatAware
    ? "These are the actual shortest stargate paths used by the solved plan. Threat-blocked systems are excluded from transit as well as pickup and delivery endpoints; historical intel is not a safety guarantee."
    : "These are the actual shortest stargate paths used by the solved plan. Every transit system obeys the selected security and manual-avoid policy.";

  const pendingRequired = new Set(
    (state.execution?.remaining_required_system_ids || []).map((systemId) => Number(systemId)),
  );
  const terminalSystemId = state.execution?.terminal_system_id === null
    || state.execution?.terminal_system_id === undefined
    ? null
    : Number(state.execution.terminal_system_id);
  const live = Boolean(state.execution);

  for (const step of travelLegs) {
    const leg = document.createElement("div");
    leg.className = "pilot-leg";
    const header = document.createElement("div");
    header.className = "pilot-leg-header";
    const title = document.createElement("strong");
    title.textContent = `Leg ${String(step.sequence).padStart(2, "0")} · ${fmtNumber(step.jump_count)} jump${Number(step.jump_count) === 1 ? "" : "s"}`;
    const targetGroup = document.createElement("div");
    targetGroup.className = "pilot-leg-target";
    const target = document.createElement("span");
    const destination = step.to_system_name || step.to_system_id;
    if (step.kind === "pickup" || step.kind === "delivery") {
      target.textContent = `${step.kind.toUpperCase()} #${step.contract_id} · ${destination}`;
    } else if (step.kind === "waypoint") {
      target.textContent = `REQUIRED WAYPOINT · ${destination}`;
    } else if (plan.model?.return_to_start) {
      target.textContent = `RETURN TO START · ${destination}`;
    } else {
      target.textContent = `FINISH · ${destination}`;
    }
    targetGroup.append(target);

    const destinationId = Number(step.to_system_id);
    const markerNeeded = step.kind === "waypoint"
      ? pendingRequired.has(destinationId)
      : step.kind === "finish" && terminalSystemId === destinationId
        && Number(state.execution?.current_system_id) !== destinationId;
    if (live && markerNeeded) {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "row-action route-system-action";
      marker.textContent = "Mark reached";
      marker.addEventListener("click", () => recordRouteSystem(destinationId, String(destination)));
      targetGroup.append(marker);
    }
    header.append(title, targetGroup);

    const path = document.createElement("div");
    path.className = "pilot-path";
    const systems = step.jump_path_systems || (step.jump_path || []).map((systemId) => ({
      system_id: systemId,
      name: String(systemId),
      security_status: null,
      security_band: null,
    }));
    systems.forEach((system, index) => {
      if (index > 0) {
        const arrow = document.createElement("span");
        arrow.className = "pilot-arrow";
        arrow.textContent = "→";
        path.append(arrow);
      }
      const node = document.createElement("span");
      node.className = `pilot-system${system.security_band ? ` ${system.security_band}` : ""}`;
      const name = document.createElement("strong");
      name.textContent = system.name || String(system.system_id);
      node.append(name);
      if (system.security_status !== null && system.security_status !== undefined) {
        const security = document.createElement("small");
        security.textContent = Number(system.security_status).toFixed(2);
        node.append(security);
      }
      path.append(node);
    });
    leg.append(header, path);
    legs.append(leg);
  }
}

function renderPlan(plan) {
  state.plan = plan;
  renderProof(plan);
  renderRoute(plan);
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

function renderExecution(execution) {
  state.execution = execution;
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
  const locked = $("#collateral-mode").value === "locked";
  $("#locked-confirm-row").classList.toggle("hidden", !locked);
  renderRoute(state.plan);
  updateButtons();
}

function renderRank(payload) {
  state.rank = payload;
  const items = payload?.items || [];
  $("#rank-empty").classList.toggle("hidden", items.length > 0);
  $("#rank-wrap").classList.toggle("hidden", items.length === 0);
  $("#rank-count").textContent = `${items.length} shown · ${fmtNumber(payload?.scope?.eligible_contracts || 0)} eligible`;
  const tbody = $("#rank-body");
  tbody.replaceChildren();
  for (const item of items) {
    const tr = document.createElement("tr");
    const contractTd = document.createElement("td");
    contractTd.className = "contract-cell";
    const title = document.createElement("strong");
    title.textContent = item.title || `Contract ${item.contract_id}`;
    title.title = item.title || `Contract ${item.contract_id}`;
    const id = document.createElement("small");
    id.textContent = `#${item.contract_id}`;
    contractTd.append(title, id);
    tr.append(contractTd);
    tr.append(makeCell(`${item.origin} → ${item.destination}`));
    tr.append(makeCell(fmtISK(item.reward_isk), "reward-cell"));
    tr.append(makeCell(`${fmtNumber(item.volume_m3, 3)} m³`));
    tr.append(makeCell(fmtISK(item.collateral_isk)));
    tr.append(makeCell(fmtNumber(item.solo_jumps)));
    tr.append(makeCell(fmtISK(item.reward_per_hour_isk)));
    tr.append(makeCell(Number(item.reward_to_collateral).toFixed(4)));
    tbody.append(tr);
  }
}

function hydratePlannerFromPlan(plan) {
  const model = plan?.model;
  if (!model) return;

  const start = $("#start");
  if (model.start_system_id !== null && model.start_system_id !== undefined) {
    start.value = model.start_system_name || String(model.start_system_id);
    start.dataset.systemId = String(model.start_system_id);
  }
  if (model.cargo_capacity_m3 !== null && model.cargo_capacity_m3 !== undefined) {
    $("#cargo-m3").value = String(model.cargo_capacity_m3);
  }

  const collateralText = String(model.collateral_budget_isk ?? "");
  let collateralValue = collateralText;
  let collateralUnit = "isk";
  if (/^\d+$/.test(collateralText)) {
    const amount = BigInt(collateralText);
    for (const [unit, factor] of [["b", 1_000_000_000n], ["m", 1_000_000n], ["k", 1_000n]]) {
      if (amount >= factor && amount % factor === 0n) {
        collateralValue = String(amount / factor);
        collateralUnit = unit;
        break;
      }
    }
  }
  $("#collateral-isk").value = collateralValue;
  $("#collateral-unit").value = collateralUnit;

  const horizonSeconds = Number(model.horizon_seconds || 0);
  $("#duration-hours").value = String(Math.floor(horizonSeconds / 3600));
  $("#duration-minutes").value = String(Math.floor((horizonSeconds % 3600) / 60));
  if (model.collateral_mode) $("#collateral-mode").value = String(model.collateral_mode);
  $("#max-simultaneous-contracts").value = model.max_simultaneous_contracts === null
    || model.max_simultaneous_contracts === undefined
    ? ""
    : String(model.max_simultaneous_contracts);

  const allowedBands = new Set(model.allowed_security_bands || []);
  $$('.security-option input[name="security-band"]').forEach((input) => {
    input.checked = allowedBands.has(input.value);
  });
  state.avoidedSystems = (model.avoided_systems || []).map((system) => ({
    id: system.id,
    name: system.name,
  }));
  state.requiredSystems = (model.required_systems || []).map((system) => ({
    id: system.id,
    name: system.name,
  }));

  $("#return-to-start").checked = Boolean(model.return_to_start);
  const finish = $("#finish-system");
  if (model.finish_system_id !== null && model.finish_system_id !== undefined) {
    finish.value = model.finish_system_name || String(model.finish_system_id);
    finish.dataset.systemId = String(model.finish_system_id);
  } else {
    finish.value = "";
    delete finish.dataset.systemId;
  }

  const threatCategories = new Set(model.threat_categories || []);
  const threatEnabled = threatCategories.size > 0;
  $("#gank-awareness").checked = threatEnabled;
  $$('input[name="threat-category"]').forEach((input) => {
    input.checked = threatCategories.has(input.value);
  });
  $("#gank-settings").classList.toggle("hidden", !threatEnabled);
  if (model.threat_min_events !== null && model.threat_min_events !== undefined) {
    $("#threat-min-events").value = String(model.threat_min_events);
  }
  if (model.threat_window_seconds) {
    $("#threat-window-hours").value = String(Number(model.threat_window_seconds) / 3600);
  }
  if (model.threat_gate_radius_m) {
    $("#threat-gate-radius-km").value = String(Number(model.threat_gate_radius_m) / 1000);
  }
  if (model.seconds_per_jump) $("#seconds-per-jump").value = String(model.seconds_per_jump);
  if (model.service_seconds !== null && model.service_seconds !== undefined) {
    $("#service-seconds").value = String(model.service_seconds);
  }

  renderAvoidPicker();
  renderRequiredPicker();
  updateRouteShapeControls();
  updateCollateralPreview();
  updateRiskStatus();
}

async function loadStatus() {
  try {
    const payload = await api("/api/status");
    $("#sde-pill").textContent = `SDE ${payload.sde.build_number} · ${fmtNumber(payload.sde.systems)} systems`;
    renderSnapshot(payload.snapshot);
    hydratePlannerFromPlan(payload.plan);
    renderPlan(payload.plan);
    renderExecution(payload.execution);
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
  if (state.regionScope === "selected" && !state.selectedRegions.length) {
    showNotice("error", "Region required.", "Search and add at least one region, or choose a region preset.");
    return;
  }
  const body = state.regionScope === "selected"
    ? { region_scope: "selected", regions: state.selectedRegions.map((item) => item.id || item.name) }
    : { region_scope: state.regionScope };
  if (state.regionScope === "security" || state.regionScope === "empire") {
    body.security_bands = plannerPayload().security_bands;
  }
  body.include_threat_intel = $("#gank-awareness").checked;
  body.threat_window_hours = $("#threat-window-hours").value;
  body.threat_gate_radius_km = $("#threat-gate-radius-km").value;
  if (body.include_threat_intel) {
    const planning = plannerPayload();
    body.threat_scope_to_plan = true;
    body.start = planning.start;
    body.duration_hours = planning.duration_hours;
    body.duration_minutes = planning.duration_minutes;
    body.security_bands = planning.security_bands;
    body.seconds_per_jump = planning.seconds_per_jump;
  }
  const result = await withBusy(
    "Scanning public courier contracts…",
    state.regionScope === "all"
      ? "Scanning all contract regions with bounded ESI concurrency; gate intel is limited to the proof-safe reachable route envelope."
      : state.regionScope === "security"
        ? "Skipping SDE regions that contain no system in the selected security bands; mixed regions are retained."
        : "Using bounded ESI concurrency and cache/rate-limit handling; gate intel covers every region this configured route could traverse.",
    () => api("/api/scan", { body }),
  );
  if (!result) return;
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
    () => api("/api/rank", { body: plannerPayload() }),
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
    () => api("/api/solve", { body: plannerPayload() }),
  );
  if (!result) return;
  state.pendingArm = false;
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
  const locked = $("#collateral-mode").value === "locked";
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
    () => api("/api/replan", { body: { ...plannerPayload(), refresh: true } }),
  );
  if (!result) return;
  state.pendingArm = $("#collateral-mode").value === "locked"
    && (result.plan.summary?.selected_contract_ids?.length || 0) > 0;
  renderSnapshot(result.snapshot);
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
  } else {
    showNotice("success", "Rolling-mode replan ready.", "Existing accepted shipments remain mandatory; future optional jobs become commitments only when you record their pickups.");
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
  renderExecution(null);
  $("#start-execution").textContent = "Arm this plan for execution";
  if (canEndSafely) {
    showNotice("success", "Execution ended.", "Planning is unlocked. Scan fresh market data or solve again from the current snapshot.");
  } else {
    showNotice("warning", "Live session reset.", "The optimizer no longer treats previously recorded commitments as mandatory. Verify your in-game contracts before solving again.");
  }
}

function sameSelection(left, right) {
  if (left.id !== null && left.id !== undefined && right.id !== null && right.id !== undefined) {
    return String(left.id) === String(right.id);
  }
  return String(left.name).toLocaleLowerCase() === String(right.name).toLocaleLowerCase();
}

function selectionChip(item, onRemove, extraClass = "") {
  const chip = document.createElement("span");
  chip.className = `selection-chip${extraClass ? ` ${extraClass}` : ""}`;
  const name = document.createElement("span");
  name.textContent = item.name;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove ${item.name}`);
  remove.textContent = "×";
  remove.addEventListener("click", onRemove);
  chip.append(name, remove);
  return chip;
}

function renderRegionPicker() {
  const chips = $("#region-chips");
  chips.replaceChildren();
  const preset = state.regionScope !== "selected";
  const labels = {
    security: "Security-compatible regions",
    empire: "NPC Empire space",
    all: "All SDE regions",
  };
  if (preset) {
    chips.append(selectionChip({ name: labels[state.regionScope] }, () => setRegionScope("selected"), "all"));
  } else {
    state.selectedRegions.forEach((item) => {
      chips.append(selectionChip(item, () => {
        state.selectedRegions = state.selectedRegions.filter((candidate) => !sameSelection(candidate, item));
        renderRegionPicker();
      }));
    });
  }
  $("#region-search").disabled = preset;
  $("#clear-regions").disabled = !preset && state.selectedRegions.length === 0;
  $("#security-regions").disabled = state.regionScope === "security";
  $("#empire-regions").disabled = state.regionScope === "empire";
  $("#all-regions").disabled = state.regionScope === "all";
  if (state.regionScope === "all") {
    $("#region-scope-help").textContent = "Every SDE region is in contract scope; this is the broadest and slowest preset.";
  } else if (state.regionScope === "security") {
    const bands = $$('input[name="security-band"]:checked').map((input) => input.value).join(" + ");
    $("#region-scope-help").textContent = `Only regions containing ${bands || "selected"} systems are scanned; mixed-security regions are kept, so this does not drop an eligible pickup region.`;
  } else if (state.regionScope === "empire") {
    $("#region-scope-help").textContent = "SDE faction-owned high/low Empire regions only; player-sovereign and NPC nullsec are excluded.";
  } else {
    $("#region-scope-help").textContent = `${state.selectedRegions.length} region${state.selectedRegions.length === 1 ? "" : "s"} selected exactly.`;
  }
}

function setRegionScope(scope) {
  state.regionScope = scope;
  renderRegionPicker();
  if (scope === "all") {
    showNotice(
      "warning",
      "All-region scope selected.",
      "The next contract scan covers every SDE region with bounded concurrency. Gate intel is scoped to systems your current security/time settings can reach.",
    );
  } else if (scope === "security") {
    showNotice("info", "Security-compatible scope selected.", "Mixed regions are retained; regions containing no allowed-security system are skipped before ESI acquisition.");
  } else if (scope === "empire") {
    showNotice("info", "NPC Empire scope selected.", "The SDE faction owner and high/low security metadata define this preset; player-sovereign and NPC nullsec regions are excluded.");
  }
}

function renderAvoidPicker() {
  const chips = $("#avoid-chips");
  chips.replaceChildren(...state.avoidedSystems.map((item) => selectionChip(item, () => {
    state.avoidedSystems = state.avoidedSystems.filter((candidate) => !sameSelection(candidate, item));
    renderAvoidPicker();
  })));
}

function renderRequiredPicker() {
  const chips = $("#required-chips");
  chips.replaceChildren(...state.requiredSystems.map((item) => selectionChip(item, () => {
    state.requiredSystems = state.requiredSystems.filter(
      (candidate) => !sameSelection(candidate, item),
    );
    renderRequiredPicker();
  })));
}

function updateRouteShapeControls() {
  const loop = $("#return-to-start").checked;
  $("#finish-system").disabled = loop;
  $("#finish-system-help").textContent = loop
    ? "Loop enabled: the route must finish back at its start."
    : "Leave blank for an open route, or choose the system where the trip must finish.";
}

function updateCollateralPreview() {
  const raw = $("#collateral-isk").value.replaceAll(",", "").replaceAll("_", "").trim();
  const match = raw.match(/^([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([kmb]?)\s*(?:isk)?$/i);
  if (!match) {
    $("#collateral-preview").textContent = "Use a number or shorthand such as 750M or 1.5B.";
    return;
  }
  const suffix = match[2].toLowerCase();
  const unit = suffix || $("#collateral-unit").value;
  const multipliers = { isk: 1, k: 1e3, m: 1e6, b: 1e9 };
  const amount = Number(match[1]) * multipliers[unit];
  $("#collateral-preview").textContent = Number.isFinite(amount)
    ? `= ${fmtISK(amount)} · a typed suffix overrides the unit menu`
    : "Collateral is too large to preview.";
}

function updateRiskStatus() {
  const node = $("#risk-data-status");
  node.classList.remove("ready", "unavailable");
  if (!state.snapshot) {
    node.textContent = "Enable this option, then scan to capture zKill evidence.";
    return;
  }
  if (!state.snapshot.threat_intel_fetched_at) {
    node.classList.add("unavailable");
    node.textContent = "No zKill gate intel in this snapshot. Scan again while this option is enabled.";
    return;
  }
  const incomplete = state.snapshot.threat_incomplete_region_ids?.length || 0;
  node.classList.add(incomplete ? "unavailable" : "ready");
  node.textContent = `${fmtNumber(state.snapshot.gate_threat_events)} gate events from ${fmtNumber(state.snapshot.threat_killmails_seen)} killmails · ${fmtNumber(state.snapshot.threat_coverage_region_ids?.length || 0)} regions covered${incomplete ? ` · ${incomplete} incomplete` : ""} · ${new Date(state.snapshot.threat_intel_fetched_at).toLocaleString()}`;
}

function wireAutocomplete({ input, menu, endpoint, minChars, onSelect, onInput = null }) {
  let timer = null;
  let items = [];
  let activeIndex = -1;

  function close() {
    menu.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    activeIndex = -1;
  }

  function setActive(index) {
    const options = [...menu.querySelectorAll(".suggestion-option")];
    if (!options.length) return;
    activeIndex = (index + options.length) % options.length;
    options.forEach((option, optionIndex) => option.classList.toggle("active", optionIndex === activeIndex));
    options[activeIndex].scrollIntoView({ block: "nearest" });
  }

  function choose(item) {
    onSelect(item);
    close();
  }

  function renderOptions() {
    menu.replaceChildren(...items.map((item) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "suggestion-option";
      option.setAttribute("role", "option");
      const label = document.createElement("span");
      label.textContent = item.name;
      option.append(label);
      if (item.security_status !== undefined) {
        const meta = document.createElement("small");
        meta.textContent = `sec ${Number(item.security_status).toFixed(2)}`;
        option.append(meta);
      }
      option.addEventListener("mousedown", (event) => event.preventDefault());
      option.addEventListener("click", () => choose(item));
      return option;
    }));
    if (items.length) {
      menu.classList.remove("hidden");
      input.setAttribute("aria-expanded", "true");
    } else {
      close();
    }
  }

  async function query() {
    const value = input.value.trim();
    if (input.disabled || value.length < minChars) {
      close();
      return;
    }
    try {
      const result = await api(`${endpoint}?q=${encodeURIComponent(value)}`);
      items = result.items;
      activeIndex = -1;
      renderOptions();
    } catch {
      close();
    }
  }

  function queueQuery(immediate = false) {
    window.clearTimeout(timer);
    timer = window.setTimeout(query, immediate ? 0 : 130);
  }

  input.addEventListener("focus", () => queueQuery(true));
  input.addEventListener("input", () => {
    if (onInput) onInput();
    queueQuery();
  });
  input.addEventListener("blur", () => window.setTimeout(close, 120));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (menu.classList.contains("hidden")) queueQuery(true);
      else setActive(activeIndex + (event.key === "ArrowDown" ? 1 : -1));
      event.preventDefault();
    } else if (event.key === "Enter" && activeIndex >= 0 && items[activeIndex]) {
      choose(items[activeIndex]);
      event.preventDefault();
    } else if (event.key === "Escape") {
      close();
    }
  });
}

function wireSuggestions() {
  wireAutocomplete({
    input: $("#region-search"),
    menu: $("#region-options"),
    endpoint: "/api/regions",
    minChars: 0,
    onSelect: (item) => {
      if (!state.selectedRegions.some((candidate) => sameSelection(candidate, item))) {
        state.selectedRegions.push(item);
      }
      $("#region-search").value = "";
      renderRegionPicker();
    },
  });
  wireAutocomplete({
    input: $("#start"),
    menu: $("#start-options"),
    endpoint: "/api/systems",
    minChars: 2,
    onInput: () => { delete $("#start").dataset.systemId; },
    onSelect: (item) => {
      $("#start").value = item.name;
      $("#start").dataset.systemId = String(item.id);
    },
  });
  wireAutocomplete({
    input: $("#avoid-search"),
    menu: $("#avoid-options"),
    endpoint: "/api/systems",
    minChars: 2,
    onSelect: (item) => {
      if (!state.avoidedSystems.some((candidate) => sameSelection(candidate, item))) {
        state.avoidedSystems.push(item);
      }
      $("#avoid-search").value = "";
      renderAvoidPicker();
    },
  });
  wireAutocomplete({
    input: $("#required-search"),
    menu: $("#required-options"),
    endpoint: "/api/systems",
    minChars: 2,
    onSelect: (item) => {
      if (!state.requiredSystems.some((candidate) => sameSelection(candidate, item))) {
        state.requiredSystems.push(item);
      }
      $("#required-search").value = "";
      renderRequiredPicker();
    },
  });
  wireAutocomplete({
    input: $("#finish-system"),
    menu: $("#finish-options"),
    endpoint: "/api/systems",
    minChars: 2,
    onInput: () => { delete $("#finish-system").dataset.systemId; },
    onSelect: (item) => {
      $("#finish-system").value = item.name;
      $("#finish-system").dataset.systemId = String(item.id);
    },
  });
}

function wireEvents() {
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
  $("#reset-defaults").addEventListener("click", () => {
    for (const [id, value] of Object.entries(defaults)) $(`#${id}`).value = value;
    delete $("#start").dataset.systemId;
    state.regionScope = "security";
    state.selectedRegions = [];
    state.avoidedSystems = [];
    state.requiredSystems = [];
    $("#return-to-start").checked = true;
    $("#finish-system").value = "";
    delete $("#finish-system").dataset.systemId;
    $$('input[name="security-band"]').forEach((input) => { input.checked = input.value === "high"; });
    $("#gank-awareness").checked = false;
    $$('input[name="threat-category"]').forEach((input) => {
      input.checked = input.value !== "any_gate_pvp";
    });
    $("#gank-settings").classList.add("hidden");
    $("#locked-confirm").checked = false;
    renderRegionPicker();
    renderAvoidPicker();
    renderRequiredPicker();
    updateRouteShapeControls();
    updateCollateralPreview();
    updateRiskStatus();
    showNotice("info", "Defaults restored.", "Your saved snapshot and execution state were not changed.");
  });
  $("#security-regions").addEventListener("click", () => setRegionScope("security"));
  $("#empire-regions").addEventListener("click", () => setRegionScope("empire"));
  $("#all-regions").addEventListener("click", () => setRegionScope("all"));
  $("#clear-regions").addEventListener("click", () => {
    state.regionScope = "selected";
    state.selectedRegions = [];
    renderRegionPicker();
  });
  $$('input[name="security-band"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!$$('input[name="security-band"]:checked').length) {
        input.checked = true;
        showNotice("warning", "One security band is required.", "High, low and null can be combined freely, but the allowed set cannot be empty.");
      }
      if (state.regionScope === "security") renderRegionPicker();
    });
  });
  $("#gank-awareness").addEventListener("change", () => {
    const enabled = $("#gank-awareness").checked;
    $("#gank-settings").classList.toggle("hidden", !enabled);
    updateRiskStatus();
    if (enabled && !state.snapshot?.threat_intel_fetched_at) {
      showNotice("info", "Gate intel snapshot needed.", "The next scan will collect cached, rate-spaced zKill killmails for every route-reachable threat region.");
    }
  });
  $$('input[name="threat-category"]').forEach((input) => {
    input.addEventListener("change", () => {
      if ($("#gank-awareness").checked && !$$('input[name="threat-category"]:checked').length) {
        input.checked = true;
        showNotice("warning", "One threat category is required.", "Choose the gate evidence that should create hard system avoids.");
      }
    });
  });
  $("#collateral-isk").addEventListener("input", updateCollateralPreview);
  $("#collateral-unit").addEventListener("change", updateCollateralPreview);
  $("#collateral-mode").addEventListener("change", () => renderExecution(state.execution));
  $("#return-to-start").addEventListener("change", updateRouteShapeControls);
  $("#max-candidates").addEventListener("input", () => {
    if ($("#max-candidates").value.trim()) {
      showNotice("warning", "Candidate cap enabled.", "This speeds difficult cases but prevents a global-optimality claim over all otherwise eligible contracts.");
    }
  });
  $("#planner-form").addEventListener("submit", (event) => event.preventDefault());
  wireSuggestions();
  renderRegionPicker();
  renderAvoidPicker();
  renderRequiredPicker();
  updateRouteShapeControls();
  updateCollateralPreview();
  updateRiskStatus();
}

wireEvents();
loadStatus();
window.setInterval(updateSnapshotAge, 1000);
