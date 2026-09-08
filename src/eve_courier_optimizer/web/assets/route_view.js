import { $, $$, fmtNumber, fmtISK, fmtDuration, makeCell } from "./display.js";

/** @param {string} selector @param {boolean} passed @param {string} yes @param {string} no */
function setCheck(selector, passed, yes, no) {
  const node = $(selector);
  node.textContent = `${passed ? "✓" : "△"} ${passed ? yes : no}`;
  node.classList.toggle("good", Boolean(passed));
  node.classList.toggle("warn", !passed);
}

/** @param {import("./contracts").PlanPayload | null} plan */
export function renderProof(plan) {
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

/** @param {import("./contracts").ExecutionPayload | null} execution */
function executionMap(execution) {
  /** @type {Map<number, import("./contracts").ActiveShipmentPayload>} */
  const map = new Map();
  for (const shipment of execution?.active_shipments || []) {
    map.set(Number(shipment.contract.contract_id), shipment);
  }
  return map;
}

/** @param {import("./contracts").PlanPayload | null} plan @param {import("./contracts").ExecutionPayload | null} execution @param {boolean} pendingArm */
export function renderRoute(plan, execution, pendingArm) {
  const route = plan?.route || [];
  $("#route-empty").classList.toggle("hidden", route.length > 0);
  $("#route-wrap").classList.toggle("hidden", route.length === 0);
  const tbody = $("#route-body");
  tbody.replaceChildren();
  const active = executionMap(execution);
  const completed = new Set(
    (execution?.completed_contract_ids || []).map((contractId) => Number(contractId)),
  );
  const lockedExecution = execution?.collateral_mode === "locked";
  const live = Boolean(execution);
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
      } else if (pendingArm && !shipment) {
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

  renderPilotRoute(plan, execution);
}

/** @param {import("./contracts").PlanPayload | null} plan @param {import("./contracts").ExecutionPayload | null} execution */
function renderPilotRoute(plan, execution) {
  const travelLegs = plan?.travel_legs || [];
  const panel = $("#pilot-route");
  const legs = $("#pilot-legs");
  panel.classList.toggle("hidden", travelLegs.length === 0);
  legs.replaceChildren();
  if (!plan || !travelLegs.length) return;

  const threatAware = (plan.model?.threat_categories?.length || 0) > 0;
  $("#route-policy-badge").textContent = threatAware ? "Threat-filtered transit" : "Security-filtered transit";
  $("#pilot-route-note").textContent = threatAware
    ? "These are the actual shortest stargate paths used by the solved plan. Threat-blocked systems are excluded from transit as well as pickup and delivery endpoints; historical intel is not a safety guarantee."
    : "These are the actual shortest stargate paths used by the solved plan. Every transit system obeys the selected security and manual-avoid policy.";

  const pendingRequired = new Set(
    (execution?.remaining_required_system_ids || []).map((systemId) => Number(systemId)),
  );
  const terminalSystemId = execution?.terminal_system_id === null
    || execution?.terminal_system_id === undefined
    ? null
    : Number(execution.terminal_system_id);
  const live = Boolean(execution);

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
        && Number(execution?.current_system_id) !== destinationId;
    if (live && markerNeeded) {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "row-action route-system-action";
      marker.textContent = "Mark reached";
      marker.dataset.systemId = String(destinationId);
      marker.dataset.systemName = String(destination);
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

/** @param {import("./contracts").ExecutionPayload | null} execution */
export function renderCommitments(execution) {
  const list = $("#commitment-list");
  list.replaceChildren();
  for (const shipment of execution?.active_shipments || []) {
    const item = document.createElement("div");
    item.className = "commitment-row";
    const description = document.createElement("span");
    const id = shipment.contract.contract_id;
    const destination = shipment.picked ? shipment.destination_system_name : shipment.origin_system_name;
    description.textContent = `#${id} · ${shipment.picked ? "Deliver at" : "Pick up at"} ${destination} · deadline ${new Date(shipment.deadline).toLocaleString()}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "row-action";
    const action = shipment.picked ? "delivery" : "pickup";
    button.textContent = `Record ${action} #${id}`;
    button.dataset.action = action;
    button.dataset.contractId = String(id);
    item.append(description, button);
    list.append(item);
  }
  const markers = [...(execution?.remaining_required_systems || [])];
  if (execution?.terminal_system_id && execution.current_system_id !== execution.terminal_system_id
      && !markers.some((item) => item.system_id === execution.terminal_system_id)) {
    markers.push({ system_id: execution.terminal_system_id, name: execution.terminal_system_name || String(execution.terminal_system_id) });
  }
  for (const system of markers) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "row-action";
    button.textContent = `Mark ${system.name} reached`;
    button.dataset.systemId = String(system.system_id);
    button.dataset.systemName = system.name;
    list.append(button);
  }
}

/** @param {import("./contracts").RankingPayload | null} payload */
export function renderRank(payload) {
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
    tr.append(makeCell(item.reward_to_collateral === null ? "--" : Number(item.reward_to_collateral).toFixed(4)));
    tbody.append(tr);
  }
}
