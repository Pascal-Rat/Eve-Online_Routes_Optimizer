/**
 * @template {keyof import('./dom_types').ElementIds} K
 * @overload
 * @param {K} selector
 * @returns {import('./dom_types').ElementIds[K]}
 */
/** @overload @param {string} selector @returns {HTMLElement} */
/** @param {string} selector @returns {HTMLElement} */
export function $(selector) {
  const element = document.querySelector(selector);
  if (!(element instanceof HTMLElement)) throw new Error(`Missing UI element: ${selector}`);
  return element;
}
/** @param {string} selector @returns {HTMLElement[]} */
export function $$(selector) {
  return [...document.querySelectorAll(selector)].map((element) => {
    if (!(element instanceof HTMLElement)) throw new Error(`Invalid UI element: ${selector}`);
    return element;
  });
}
/** @param {string} selector @returns {HTMLInputElement[]} */
export function inputs(selector) {
  return $$(selector).map((element) => {
    if (!(element instanceof HTMLInputElement)) throw new Error(`Invalid input: ${selector}`);
    return element;
  });
}
/** @param {string} selector @returns {HTMLInputElement | HTMLSelectElement} */
export function field(selector) {
  const element = $(selector);
  if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement)) {
    throw new Error(`Invalid form field: ${selector}`);
  }
  return element;
}

/** @param {unknown} value @param {number} [digits] */
export function fmtNumber(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(number);
}

/** @param {unknown} value @param {boolean} [compact] */
export function fmtISK(value, compact = true) {
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

/** @param {number | null | undefined} seconds */
export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "--";
  const total = Math.max(0, Number(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

/** @param {string} kind @param {string} title @param {string} detail */
export function showNotice(kind, title, detail) {
  const notice = $("#notice");
  notice.className = `notice ${kind}`;
  $("#notice .notice-icon").textContent = kind === "error" ? "!" : kind === "success" ? "✓" : kind === "warning" ? "!" : "i";
  $("#notice div").replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = `${title} `;
  $("#notice div").append(strong, document.createTextNode(detail));
}

/** @param {string} text @param {string} [className] */
export function makeCell(text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}
