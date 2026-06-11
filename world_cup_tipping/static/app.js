function safeStorageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeStorageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Theme persistence is a nicety; the UI should keep working without storage.
  }
}

function setInitialTheme() {
  const storedTheme = safeStorageGet("theme");
  const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const prefersDark = Boolean(media && media.matches);
  document.documentElement.dataset.theme = storedTheme || (prefersDark ? "dark" : "light");
  updateThemeButton();
}

function updateThemeButton() {
  const button = document.querySelector("[data-theme-toggle]");
  if (!button) {
    return;
  }
  button.textContent = document.documentElement.dataset.theme === "dark" ? "Light" : "Dark";
}

function initializeThemeToggle() {
  const button = document.querySelector("[data-theme-toggle]");
  if (!button) {
    return;
  }
  button.addEventListener("click", () => {
    const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = nextTheme;
    safeStorageSet("theme", nextTheme);
    updateThemeButton();
  });
}

function formatTimes() {
  document.querySelectorAll("time[data-utc]").forEach((node) => {
    const value = node.getAttribute("data-utc");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return;
    }
    node.textContent = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  });
}

function normalize(value) {
  return String(value || "").trim().toLowerCase();
}

function sortableRows(table) {
  return [...table.querySelectorAll("tbody tr")].filter((row) => row.hasAttribute("data-search"));
}

function getSearchInput(table) {
  return document.querySelector(`[data-table-search="${table.id}"]`);
}

function getFilterInputs(table) {
  return [...document.querySelectorAll(`[data-table-filter="${table.id}"]`)];
}

function getFilterValue(row, key) {
  return row.getAttribute(`data-${key}`) || "";
}

function getAttachedDetailRow(row) {
  const detailRowId = row.getAttribute("data-detail-row");
  return detailRowId ? document.getElementById(detailRowId) : null;
}

function isRowExpanded(row) {
  return row.querySelector("[data-fixture-toggle]")?.getAttribute("aria-expanded") === "true";
}

function setExpandableRow(row, expanded) {
  const toggle = row.querySelector("[data-fixture-toggle]");
  const detailRow = getAttachedDetailRow(row);
  row.classList.toggle("is-expanded", expanded);
  if (toggle) {
    toggle.setAttribute("aria-expanded", String(expanded));
  }
  if (detailRow) {
    detailRow.hidden = !expanded || row.style.display === "none";
  }
}

function applyTableFilters(table) {
  const query = normalize(getSearchInput(table)?.value);
  const filters = getFilterInputs(table);
  let visibleCount = 0;

  sortableRows(table).forEach((row) => {
    const searchable = normalize(row.getAttribute("data-search"));
    const matchesSearch = !query || searchable.includes(query);
    const matchesFilters = filters.every((filter) => {
      const value = filter.value;
      const key = filter.getAttribute("data-filter-key");
      return !value || getFilterValue(row, key) === value;
    });
    const visible = matchesSearch && matchesFilters;
    row.style.display = visible ? "" : "none";
    const detailRow = getAttachedDetailRow(row);
    if (detailRow) {
      detailRow.hidden = !visible || !isRowExpanded(row);
    }
    if (visible) {
      visibleCount += 1;
    }
  });

  table.closest(".table-wrap")?.setAttribute("data-visible-rows", String(visibleCount));
}

function compareRows(a, b, key, type, direction) {
  const aValue = a.getAttribute(`data-sort-${key}`) || "";
  const bValue = b.getAttribute(`data-sort-${key}`) || "";
  let result;

  if (type === "number") {
    result = (Number.parseFloat(aValue) || 0) - (Number.parseFloat(bValue) || 0);
  } else if (type === "date") {
    const aTime = new Date(aValue).getTime() || 0;
    const bTime = new Date(bValue).getTime() || 0;
    result = aTime - bTime;
  } else {
    result = aValue.localeCompare(bValue, undefined, { numeric: true, sensitivity: "base" });
  }

  return direction === "desc" ? -result : result;
}

function sortTable(table, button) {
  const key = button.getAttribute("data-sort-key");
  const type = button.getAttribute("data-sort-type") || "text";
  const direction = button.getAttribute("data-direction") === "asc" ? "desc" : "asc";
  const body = table.querySelector("tbody");

  table.querySelectorAll("[data-sort-key]").forEach((item) => {
    item.removeAttribute("data-direction");
  });
  button.setAttribute("data-direction", direction);

  sortableRows(table)
    .sort((a, b) => compareRows(a, b, key, type, direction))
    .forEach((row) => {
      body.appendChild(row);
      const detailRow = getAttachedDetailRow(row);
      if (detailRow) {
        body.appendChild(detailRow);
      }
    });

  applyTableFilters(table);
}

function initializeTable(table) {
  table.querySelectorAll("[data-sort-key]").forEach((button) => {
    button.addEventListener("click", () => sortTable(table, button));
  });

  const searchInput = getSearchInput(table);
  if (searchInput) {
    searchInput.addEventListener("input", () => applyTableFilters(table));
  }
  getFilterInputs(table).forEach((filter) => {
    filter.addEventListener("change", () => applyTableFilters(table));
  });

  applyTableFilters(table);
}

function initializeExpandableRows() {
  document.querySelectorAll("[data-expandable-row]").forEach((row) => {
    const toggle = row.querySelector("[data-fixture-toggle]");
    if (toggle) {
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        setExpandableRow(row, !isRowExpanded(row));
      });
    }

    row.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest("a, button, input, select, textarea, label")) {
        return;
      }
      setExpandableRow(row, !isRowExpanded(row));
    });

    row.addEventListener("keydown", (event) => {
      if (event.target !== row) {
        return;
      }
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      event.preventDefault();
      setExpandableRow(row, !isRowExpanded(row));
    });
  });
}

function initializeAutoSubmitControls() {
  document.querySelectorAll("[data-auto-submit]").forEach((control) => {
    control.addEventListener("change", () => {
      if (control.form) {
        control.form.submit();
      }
    });
  });
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function resultKey(scoreA, scoreB) {
  if (scoreA > scoreB) {
    return "team_a";
  }
  if (scoreB > scoreA) {
    return "team_b";
  }
  return "draw";
}

function validateApiPrediction(requestPayload, responseJson) {
  if (!requestPayload || typeof requestPayload !== "object" || Array.isArray(requestPayload)) {
    return { valid: false, prediction: null, error: "Request payload must be a JSON object." };
  }
  const stage = requestPayload.stage;
  const teamA = requestPayload.team_a;
  const teamB = requestPayload.team_b;
  if (typeof stage !== "string" || typeof teamA !== "string" || typeof teamB !== "string") {
    return { valid: false, prediction: null, error: "Request payload must include stage, team_a, and team_b." };
  }
  if (!responseJson || typeof responseJson !== "object" || Array.isArray(responseJson)) {
    return { valid: false, prediction: null, error: "Response JSON must be an object." };
  }

  const scoreA = responseJson.predicted_score_a;
  const scoreB = responseJson.predicted_score_b;
  if (!Number.isInteger(scoreA) || !Number.isInteger(scoreB)) {
    return { valid: false, prediction: null, error: "Predicted scores must be integers." };
  }
  if (scoreA < 0 || scoreB < 0) {
    return { valid: false, prediction: null, error: "Predicted scores must be non-negative." };
  }

  const confidence = responseJson.confidence;
  if (confidence !== undefined && confidence !== null) {
    if (typeof confidence !== "number" || Number.isNaN(confidence)) {
      return { valid: false, prediction: null, error: "Confidence must be a number." };
    }
    if (confidence < 0 || confidence > 1) {
      return { valid: false, prediction: null, error: "Confidence must be between 0 and 1." };
    }
  }

  let predictedWinner = responseJson.predicted_winner;
  if (predictedWinner === "") {
    predictedWinner = null;
  }
  const knockoutStages = new Set(["round_of_32", "round_of_16", "quarterfinal", "semifinal", "third_place", "final"]);
  const winnerRequired = knockoutStages.has(stage) || resultKey(scoreA, scoreB) !== "draw";
  if (winnerRequired && predictedWinner !== teamA && predictedWinner !== teamB) {
    return { valid: false, prediction: null, error: "Predicted winner must be one of the fixture teams." };
  }
  if (!winnerRequired && predictedWinner !== null && predictedWinner !== undefined && predictedWinner !== teamA && predictedWinner !== teamB) {
    return { valid: false, prediction: null, error: "Predicted winner must be empty or one of the fixture teams." };
  }

  return {
    valid: true,
    prediction: {
      predicted_score_a: scoreA,
      predicted_score_b: scoreB,
      predicted_winner: predictedWinner || null,
      confidence: confidence === undefined ? null : confidence,
    },
    error: null,
  };
}

function setApiTestStatus(root, label, ok) {
  const status = root.querySelector("[data-api-test-status]");
  if (!status) {
    return;
  }
  status.textContent = label;
  status.classList.toggle("is-ok", Boolean(ok));
  status.classList.toggle("is-warning", !ok);
}

function setApiTestText(root, selector, text) {
  const node = root.querySelector(selector);
  if (node) {
    node.textContent = text;
  }
}

function setApiTestError(root, message) {
  const node = root.querySelector("[data-api-test-error]");
  if (!node) {
    return;
  }
  node.hidden = !message;
  node.textContent = message || "";
}

async function sendApiTest(root) {
  const endpointUrl = root.getAttribute("data-endpoint-url");
  const payloadInput = root.querySelector("[data-api-test-payload]");
  if (!endpointUrl || !payloadInput) {
    return;
  }

  let requestPayload;
  try {
    requestPayload = JSON.parse(payloadInput.value);
  } catch (error) {
    setApiTestStatus(root, "check", false);
    setApiTestError(root, `JSONDecodeError: ${error.message}`);
    setApiTestText(root, "[data-api-test-http]", "");
    setApiTestText(root, "[data-api-test-validation]", "Request payload is not valid JSON.");
    setApiTestText(root, "[data-api-test-json]", "No JSON object parsed.");
    setApiTestText(root, "[data-api-test-raw]", "No response body.");
    return;
  }

  setApiTestStatus(root, "sending", false);
  setApiTestError(root, "");
  setApiTestText(root, "[data-api-test-http]", "");
  setApiTestText(root, "[data-api-test-validation]", "Waiting for response...");
  setApiTestText(root, "[data-api-test-json]", "No JSON object parsed.");
  setApiTestText(root, "[data-api-test-raw]", "No response body.");

  let response;
  let bodyText = "";
  let responseJson = null;
  try {
    response = await fetch(endpointUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload),
      credentials: "omit",
    });
    bodyText = await response.text();
    if (bodyText) {
      try {
        responseJson = JSON.parse(bodyText);
      } catch {
        responseJson = null;
      }
    }
  } catch (error) {
    setApiTestStatus(root, "check", false);
    setApiTestError(root, `${error.name}: ${error.message}`);
    setApiTestText(root, "[data-api-test-validation]", "No response JSON object to validate.");
    return;
  }

  const validation = validateApiPrediction(requestPayload, responseJson);
  const ok = response.ok && validation.valid;
  setApiTestStatus(root, ok ? "valid" : "check", ok);
  setApiTestText(root, "[data-api-test-http]", `HTTP ${response.status}`);
  setApiTestError(root, response.ok ? validation.error : `${response.status} ${response.statusText}`);
  setApiTestText(root, "[data-api-test-validation]", validation.valid ? prettyJson(validation.prediction) : validation.error);
  setApiTestText(root, "[data-api-test-json]", responseJson ? prettyJson(responseJson) : "No JSON object parsed.");
  setApiTestText(root, "[data-api-test-raw]", bodyText || "No response body.");
}

function initializeApiTester() {
  document.querySelectorAll("[data-api-test]").forEach((root) => {
    const button = root.querySelector("[data-api-test-send]");
    if (button) {
      button.addEventListener("click", () => sendApiTest(root));
    }
  });
}

function initializeBracketScroll() {
  document.querySelectorAll("[data-bracket-scroll]").forEach((container) => {
    if (container.scrollWidth <= container.clientWidth) {
      return;
    }
    const center = container.querySelector(".bracket-center");
    const centeredScrollLeft = center
      ? center.offsetLeft + center.offsetWidth / 2 - container.clientWidth / 2
      : (container.scrollWidth - container.clientWidth) / 2;
    container.scrollLeft = Math.max(0, centeredScrollLeft);
  });
}

setInitialTheme();
initializeThemeToggle();
formatTimes();
initializeAutoSubmitControls();
initializeApiTester();
initializeBracketScroll();
initializeExpandableRows();
document.querySelectorAll("[data-sortable-table]").forEach(initializeTable);
