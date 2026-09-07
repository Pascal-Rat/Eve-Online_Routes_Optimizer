export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export function fmtNumber(value, digits = 0) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(number);
}

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

export function showNotice(kind, title, detail) {
  const notice = $("#notice");
  notice.className = `notice ${kind}`;
  notice.querySelector(".notice-icon").textContent = kind === "error" ? "!" : kind === "success" ? "✓" : kind === "warning" ? "!" : "i";
  notice.querySelector("div").replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = `${title} `;
  notice.querySelector("div").append(strong, document.createTextNode(detail));
}

export function makeCell(text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}
