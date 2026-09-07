import { $, $$, fmtNumber, fmtISK, showNotice } from "./display.js";
import { wireAutocomplete } from "./autocomplete.js";

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

export class PlanningForm {
  constructor() {
    this.regionScope = "security";
    this.selectedRegions = [];
    this.avoidedSystems = [];
    this.requiredSystems = [];
    this.snapshot = null;
    this.wireEvents();
  }

  plannerPayload() {
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
      avoid_systems: this.avoidedSystems.map((item) => item.id || item.name),
      required_systems: this.requiredSystems.map((item) => item.id || item.name),
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

  hydratePlannerFromPlan(plan) {
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
    this.avoidedSystems = (model.avoided_systems || []).map((system) => ({
      id: system.id,
      name: system.name,
    }));
    this.requiredSystems = (model.required_systems || []).map((system) => ({
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

    this.renderAvoidPicker();
    this.renderRequiredPicker();
    this.updateRouteShapeControls();
    this.updateCollateralPreview();
    this.updateRiskStatus();
  }

  renderRegionPicker() {
    const chips = $("#region-chips");
    chips.replaceChildren();
    const preset = this.regionScope !== "selected";
    const labels = {
      security: "Security-compatible regions",
      empire: "NPC Empire space",
      all: "All SDE regions",
    };
    if (preset) {
      chips.append(selectionChip({ name: labels[this.regionScope] }, () => this.setRegionScope("selected"), "all"));
    } else {
      this.selectedRegions.forEach((item) => {
        chips.append(selectionChip(item, () => {
          this.selectedRegions = this.selectedRegions.filter((candidate) => !sameSelection(candidate, item));
          this.renderRegionPicker();
        }));
      });
    }
    $("#region-search").disabled = preset;
    $("#clear-regions").disabled = !preset && this.selectedRegions.length === 0;
    $("#security-regions").disabled = this.regionScope === "security";
    $("#empire-regions").disabled = this.regionScope === "empire";
    $("#all-regions").disabled = this.regionScope === "all";
    if (this.regionScope === "all") {
      $("#region-scope-help").textContent = "Every SDE region is in contract scope; this is the broadest and slowest preset.";
    } else if (this.regionScope === "security") {
      const bands = $$('input[name="security-band"]:checked').map((input) => input.value).join(" + ");
      $("#region-scope-help").textContent = `Only regions containing ${bands || "selected"} systems are scanned; mixed-security regions are kept, so this does not drop an eligible pickup region.`;
    } else if (this.regionScope === "empire") {
      $("#region-scope-help").textContent = "SDE faction-owned high/low Empire regions only; player-sovereign and NPC nullsec are excluded.";
    } else {
      $("#region-scope-help").textContent = `${this.selectedRegions.length} region${this.selectedRegions.length === 1 ? "" : "s"} selected exactly.`;
    }
  }

  setRegionScope(scope) {
    this.regionScope = scope;
    this.renderRegionPicker();
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

  renderAvoidPicker() {
    const chips = $("#avoid-chips");
    chips.replaceChildren(...this.avoidedSystems.map((item) => selectionChip(item, () => {
      this.avoidedSystems = this.avoidedSystems.filter((candidate) => !sameSelection(candidate, item));
      this.renderAvoidPicker();
    })));
  }

  renderRequiredPicker() {
    const chips = $("#required-chips");
    chips.replaceChildren(...this.requiredSystems.map((item) => selectionChip(item, () => {
      this.requiredSystems = this.requiredSystems.filter(
        (candidate) => !sameSelection(candidate, item),
      );
      this.renderRequiredPicker();
    })));
  }

  updateRouteShapeControls() {
    const loop = $("#return-to-start").checked;
    $("#finish-system").disabled = loop;
    $("#finish-system-help").textContent = loop
      ? "Loop enabled: the route must finish back at its start."
      : "Leave blank for an open route, or choose the system where the trip must finish.";
  }

  updateCollateralPreview() {
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

  updateRiskStatus() {
    const node = $("#risk-data-status");
    node.classList.remove("ready", "unavailable");
    if (!this.snapshot) {
      node.textContent = "Enable this option, then scan to capture zKill evidence.";
      return;
    }
    if (!this.snapshot.threat_intel_fetched_at) {
      node.classList.add("unavailable");
      node.textContent = "No zKill gate intel in this snapshot. Scan again while this option is enabled.";
      return;
    }
    const incomplete = this.snapshot.threat_incomplete_region_ids?.length || 0;
    node.classList.add(incomplete ? "unavailable" : "ready");
    node.textContent = `${fmtNumber(this.snapshot.gate_threat_events)} gate events from ${fmtNumber(this.snapshot.threat_killmails_seen)} killmails · ${fmtNumber(this.snapshot.threat_coverage_region_ids?.length || 0)} regions covered${incomplete ? ` · ${incomplete} incomplete` : ""} · ${new Date(this.snapshot.threat_intel_fetched_at).toLocaleString()}`;
  }

  wireSuggestions() {
    wireAutocomplete({
      input: $("#region-search"),
      menu: $("#region-options"),
      endpoint: "/api/regions",
      minChars: 0,
      onSelect: (item) => {
        if (!this.selectedRegions.some((candidate) => sameSelection(candidate, item))) {
          this.selectedRegions.push(item);
        }
        $("#region-search").value = "";
        this.renderRegionPicker();
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
        if (!this.avoidedSystems.some((candidate) => sameSelection(candidate, item))) {
          this.avoidedSystems.push(item);
        }
        $("#avoid-search").value = "";
        this.renderAvoidPicker();
      },
    });
    wireAutocomplete({
      input: $("#required-search"),
      menu: $("#required-options"),
      endpoint: "/api/systems",
      minChars: 2,
      onSelect: (item) => {
        if (!this.requiredSystems.some((candidate) => sameSelection(candidate, item))) {
          this.requiredSystems.push(item);
        }
        $("#required-search").value = "";
        this.renderRequiredPicker();
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

  wireEvents() {
    $("#reset-defaults").addEventListener("click", () => {
      for (const [id, value] of Object.entries(defaults)) $(`#${id}`).value = value;
      delete $("#start").dataset.systemId;
      this.regionScope = "security";
      this.selectedRegions = [];
      this.avoidedSystems = [];
      this.requiredSystems = [];
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
      this.renderRegionPicker();
      this.renderAvoidPicker();
      this.renderRequiredPicker();
      this.updateRouteShapeControls();
      this.updateCollateralPreview();
      this.updateRiskStatus();
      showNotice("info", "Defaults restored.", "Your saved snapshot and execution state were not changed.");
    });
    $("#security-regions").addEventListener("click", () => this.setRegionScope("security"));
    $("#empire-regions").addEventListener("click", () => this.setRegionScope("empire"));
    $("#all-regions").addEventListener("click", () => this.setRegionScope("all"));
    $("#clear-regions").addEventListener("click", () => {
      this.regionScope = "selected";
      this.selectedRegions = [];
      this.renderRegionPicker();
    });
    $$('input[name="security-band"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (!$$('input[name="security-band"]:checked').length) {
          input.checked = true;
          showNotice("warning", "One security band is required.", "High, low and null can be combined freely, but the allowed set cannot be empty.");
        }
        if (this.regionScope === "security") this.renderRegionPicker();
      });
    });
    $("#gank-awareness").addEventListener("change", () => {
      const enabled = $("#gank-awareness").checked;
      $("#gank-settings").classList.toggle("hidden", !enabled);
      this.updateRiskStatus();
      if (enabled && !this.snapshot?.threat_intel_fetched_at) {
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
    $("#collateral-isk").addEventListener("input", () => this.updateCollateralPreview());
    $("#collateral-unit").addEventListener("change", () => this.updateCollateralPreview());
    $("#return-to-start").addEventListener("change", () => this.updateRouteShapeControls());
    $("#max-candidates").addEventListener("input", () => {
      if ($("#max-candidates").value.trim()) {
        showNotice("warning", "Candidate cap enabled.", "This speeds difficult cases but prevents a global-optimality claim over all otherwise eligible contracts.");
      }
    });
    $("#planner-form").addEventListener("submit", (event) => event.preventDefault());
    this.wireSuggestions();
    this.renderRegionPicker();
    this.renderAvoidPicker();
    this.renderRequiredPicker();
    this.updateRouteShapeControls();
    this.updateCollateralPreview();
    this.updateRiskStatus();
  }
}
