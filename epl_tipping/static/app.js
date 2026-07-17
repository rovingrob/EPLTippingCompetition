function formatUtcTimes() {
  const displayTimeZone = document.body.dataset.displayTimezone || "Australia/Sydney";
  document.querySelectorAll("time[data-utc]").forEach((element) => {
    const value = element.dataset.utc;
    if (!value) return;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return;
    element.textContent = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: displayTimeZone,
    }).format(parsed);
    element.title = value;
  });
}

function setupTableTools() {
  document.querySelectorAll("[data-table-search]").forEach((input) => {
    const table = document.getElementById(input.dataset.tableSearch);
    if (!table) return;
    input.addEventListener("input", () => filterTable(table));
  });
  document.querySelectorAll("[data-table-filter]").forEach((select) => {
    const table = document.getElementById(select.dataset.tableFilter);
    if (!table) return;
    select.addEventListener("change", () => filterTable(table));
  });
}

function filterTable(table) {
  const search = document.querySelector(`[data-table-search="${table.id}"]`);
  const query = (search?.value || "").trim().toLowerCase();
  const filters = [...document.querySelectorAll(`[data-table-filter="${table.id}"]`)];
  table.querySelectorAll("tbody tr").forEach((row) => {
    const matchesSearch = !query || (row.dataset.search || row.textContent).toLowerCase().includes(query);
    const matchesFilters = filters.every((filter) => {
      if (!filter.value) return true;
      return (row.dataset[filter.dataset.filterKey] || "") === filter.value;
    });
    row.hidden = !(matchesSearch && matchesFilters);
  });
}

function setupApiTest() {
  const root = document.querySelector("[data-api-test]");
  if (!root) return;
  const send = root.querySelector("[data-api-test-send]");
  send?.addEventListener("click", async () => {
    const status = root.querySelector("[data-api-test-status]");
    const http = root.querySelector("[data-api-test-http]");
    const validation = root.querySelector("[data-api-test-validation]");
    const jsonBox = root.querySelector("[data-api-test-json]");
    const errorBox = root.querySelector("[data-api-test-error]");
    status.textContent = "sending";
    http.textContent = "";
    errorBox.hidden = true;
    let payload;
    try {
      payload = JSON.parse(root.querySelector("[data-api-test-payload]").value);
    } catch (error) {
      status.textContent = "invalid request";
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      return;
    }
    try {
      const response = await fetch(root.dataset.endpointUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      http.textContent = `HTTP ${response.status}`;
      const text = await response.text();
      let body;
      try { body = JSON.parse(text); } catch (_) { body = null; }
      jsonBox.textContent = body ? JSON.stringify(body, null, 2) : text;
      const issues = [];
      if (!body || typeof body !== "object" || Array.isArray(body)) issues.push("Response must be a JSON object");
      for (const field of ["predicted_score_home", "predicted_score_away"]) {
        if (!Number.isInteger(body?.[field]) || body[field] < 0) issues.push(`${field} must be a non-negative integer`);
      }
      if (body?.confidence != null && (typeof body.confidence !== "number" || body.confidence < 0 || body.confidence > 1)) {
        issues.push("confidence must be between 0 and 1");
      }
      validation.textContent = issues.length ? issues.join("\n") : "Valid v1 prediction response";
      status.textContent = response.ok && !issues.length ? "valid" : "invalid";
    } catch (error) {
      status.textContent = "failed";
      errorBox.textContent = `${error.name}: ${error.message}`;
      errorBox.hidden = false;
    }
  });
}

formatUtcTimes();
setupTableTools();
setupApiTest();
