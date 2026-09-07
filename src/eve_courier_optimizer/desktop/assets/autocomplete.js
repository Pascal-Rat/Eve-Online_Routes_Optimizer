import { api } from "./api.js";

export function wireAutocomplete({ input, menu, endpoint, minChars, onSelect, onInput = null }) {
  let timer = null;
  let items = [];
  let generation = 0;
  let request = null;
  let activeIndex = -1;

  function close() {
    generation += 1;
    window.clearTimeout(timer);
    request?.abort();
    menu.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    activeIndex = -1;
    input.removeAttribute("aria-activedescendant");
  }

  function setActive(index) {
    const options = [...menu.querySelectorAll(".suggestion-option")];
    if (!options.length) return;
    activeIndex = (index + options.length) % options.length;
    options.forEach((option, optionIndex) => {
      option.classList.toggle("active", optionIndex === activeIndex);
      option.setAttribute("aria-selected", String(optionIndex === activeIndex));
    });
    input.setAttribute("aria-activedescendant", options[activeIndex].id);
    options[activeIndex].scrollIntoView({ block: "nearest" });
  }

  function choose(item) {
    onSelect(item);
    close();
  }

  function renderOptions() {
    menu.replaceChildren(...items.map((item, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "suggestion-option";
      option.setAttribute("role", "option");
      option.id = `${menu.id}-option-${index}`;
      option.setAttribute("aria-selected", "false");
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

  async function query(version) {
    const value = input.value.trim();
    if (document.activeElement !== input || input.disabled || value.length < minChars) {
      close();
      return;
    }
    request = new AbortController();
    try {
      const result = await api(`${endpoint}?q=${encodeURIComponent(value)}`, { signal: request.signal });
      if (version !== generation || document.activeElement !== input || input.disabled) return;
      items = result.items;
      activeIndex = -1;
      renderOptions();
    } catch (error) {
      if (version === generation && error.name !== "AbortError") close();
    }
  }

  function queueQuery(immediate = false) {
    close();
    const version = generation;
    timer = window.setTimeout(() => query(version), immediate ? 0 : 130);
  }

  input.addEventListener("focus", () => queueQuery(true));
  input.addEventListener("input", () => {
    if (onInput) onInput();
    queueQuery();
  });
  input.addEventListener("blur", close);
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (menu.classList.contains("hidden")) queueQuery(true);
      else if (activeIndex < 0) setActive(event.key === "ArrowDown" ? 0 : items.length - 1);
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
