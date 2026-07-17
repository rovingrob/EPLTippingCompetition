function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch (_) {
    return "UTC";
  }
}

function setupBrowserTimezone() {
  const timeZone = browserTimeZone();
  document.querySelectorAll("[data-browser-timezone]").forEach((element) => {
    element.textContent = timeZone;
  });

  const todayPage = document.querySelector("[data-today-timezone]");
  if (!todayPage || todayPage.dataset.todayTimezone === timeZone) return;
  const url = new URL(window.location.href);
  if (url.searchParams.get("tz") === timeZone) return;
  url.searchParams.set("tz", timeZone);
  window.location.replace(url);
}

function formatUtcTimes() {
  document.querySelectorAll("time[data-utc]").forEach((element) => {
    const value = element.dataset.utc;
    if (!value) return;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return;
    element.textContent = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed);
    element.title = value;
  });
}

function sortableRows(table) {
  const body = table.tBodies[0];
  return body ? [...body.children].filter((row) => row.matches("tr[data-expandable-row]")) : [];
}

function sortableButtons(table) {
  return table.tHead ? [...table.tHead.querySelectorAll("[data-sort-key]")] : [];
}

function compareRows(a, b, key, type, direction) {
  const aValue = a.getAttribute(`data-sort-${key}`) || "";
  const bValue = b.getAttribute(`data-sort-${key}`) || "";
  let result;
  if (type === "number") {
    result = (Number.parseFloat(aValue) || 0) - (Number.parseFloat(bValue) || 0);
  } else if (type === "date") {
    result = (new Date(aValue).getTime() || 0) - (new Date(bValue).getTime() || 0);
  } else {
    result = aValue.localeCompare(bValue, undefined, { numeric: true, sensitivity: "base" });
  }
  return direction === "desc" ? -result : result;
}

function sortTable(table, button) {
  const body = table.tBodies[0];
  if (!body) return;
  const key = button.dataset.sortKey;
  const type = button.dataset.sortType || "text";
  const direction = button.dataset.direction === "asc" ? "desc" : "asc";
  sortableButtons(table).forEach((item) => {
    delete item.dataset.direction;
    item.closest("th")?.removeAttribute("aria-sort");
  });
  button.dataset.direction = direction;
  button.closest("th")?.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");
  sortableRows(table)
    .sort((a, b) => compareRows(a, b, key, type, direction))
    .forEach((row) => {
      body.appendChild(row);
      const detailRow = attachedDetailRow(row);
      if (detailRow) body.appendChild(detailRow);
    });
}

function setupSortableTables() {
  document.querySelectorAll("[data-sortable-table]").forEach((table) => {
    sortableButtons(table).forEach((button) => {
      button.addEventListener("click", () => sortTable(table, button));
    });
  });
}

function setupAutoSubmit() {
  document.querySelectorAll("[data-auto-submit]").forEach((input) => {
    input.addEventListener("change", () => input.form?.requestSubmit());
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
  table.querySelectorAll("tbody tr[data-search]").forEach((row) => {
    const matchesSearch = !query || (row.dataset.search || row.textContent).toLowerCase().includes(query);
    const matchesFilters = filters.every((filter) => {
      if (!filter.value) return true;
      return (row.dataset[filter.dataset.filterKey] || "") === filter.value;
    });
    row.hidden = !(matchesSearch && matchesFilters);
    const detailRow = attachedDetailRow(row);
    if (detailRow) detailRow.hidden = row.hidden || !isExpandableRowOpen(row);
  });
}

function attachedDetailRow(row) {
  return row.dataset.detailRow ? document.getElementById(row.dataset.detailRow) : null;
}

function isExpandableRowOpen(row) {
  return row.querySelector("[data-fixture-toggle]")?.getAttribute("aria-expanded") === "true";
}

function setExpandableRow(row, expanded) {
  const toggle = row.querySelector("[data-fixture-toggle]");
  const detailRow = attachedDetailRow(row);
  row.classList.toggle("is-expanded", expanded);
  toggle?.setAttribute("aria-expanded", String(expanded));
  if (detailRow) detailRow.hidden = !expanded || row.hidden;
}

function setupExpandableRows() {
  document.querySelectorAll("[data-expandable-row]").forEach((row) => {
    const toggle = row.querySelector("[data-fixture-toggle]");
    toggle?.addEventListener("click", (event) => {
      event.stopPropagation();
      setExpandableRow(row, !isExpandableRowOpen(row));
    });
    row.addEventListener("click", (event) => {
      if (!(event.target instanceof Element) || event.target.closest("a, button, input, select, textarea, label")) return;
      setExpandableRow(row, !isExpandableRowOpen(row));
    });
    row.addEventListener("keydown", (event) => {
      if (event.target !== row || !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      setExpandableRow(row, !isExpandableRowOpen(row));
    });
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

function setSnakeActive(root, contestantId, locked) {
  root.querySelectorAll("[data-snake-contestant]").forEach((item) => {
    const active = Boolean(contestantId) && item.dataset.snakeContestant === contestantId;
    item.classList.toggle("is-active", active);
    item.classList.toggle("is-dimmed", Boolean(contestantId) && !active);
    if (item instanceof HTMLButtonElement) {
      item.setAttribute("aria-pressed", String(Boolean(locked) && active));
    }
  });

  document.querySelectorAll("[data-contestant-id]").forEach((row) => {
    row.classList.toggle("is-snake-highlight", Boolean(contestantId) && row.dataset.contestantId === contestantId);
  });
}

function lockedSnakeContestant(root) {
  return root.dataset.lockedContestant || "";
}

function setupLeaderboardSnake() {
  document.querySelectorAll("[data-leaderboard-snake]").forEach((root) => {
    root.querySelectorAll("[data-snake-contestant]").forEach((item) => {
      const contestantId = item.dataset.snakeContestant;
      if (!contestantId) return;

      item.addEventListener("mouseenter", () => {
        if (!lockedSnakeContestant(root)) setSnakeActive(root, contestantId, false);
      });
      item.addEventListener("mouseleave", () => {
        if (!lockedSnakeContestant(root)) setSnakeActive(root, "", false);
      });
      item.addEventListener("focus", () => {
        if (!lockedSnakeContestant(root)) setSnakeActive(root, contestantId, false);
      });
      item.addEventListener("blur", () => {
        if (!lockedSnakeContestant(root)) setSnakeActive(root, "", false);
      });
      item.addEventListener("click", () => {
        const nextId = lockedSnakeContestant(root) === contestantId ? "" : contestantId;
        root.dataset.lockedContestant = nextId;
        setSnakeActive(root, nextId, Boolean(nextId));
      });
    });

    root.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      root.dataset.lockedContestant = "";
      setSnakeActive(root, "", false);
    });
  });
}

setupBrowserTimezone();
formatUtcTimes();
setupTableTools();
setupSortableTables();
setupAutoSubmit();
setupExpandableRows();
setupApiTest();
setupLeaderboardSnake();
