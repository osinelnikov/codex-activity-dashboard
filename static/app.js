(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[character]));
  const colors = ["#8de0ba", "#81cbd0", "#d8b981"];
  let sessions = [];
  let current = null;
  let searchTimer;
  let sessionsRequest = 0;
  let detailRequest = 0;

  const format = value => Number(value || 0).toLocaleString("en-US");
  const tokenFormat = value => value == null ? "N/A" : format(value);
  const duration = seconds => seconds == null
    ? "N/A"
    : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;

  function clearSession(message) {
    current = null;
    ++detailRequest;
    $("title").textContent = message;
    $("session-id").textContent = "—";
    $("subtitle").textContent = "Local Codex session metadata";
    $("now").textContent = "NO DATA";
    $("freshness").textContent = "—";
    $("model").textContent = "MODEL — · EFFORT —";
    ["token-stats", "token-chart", "token-legend", "time-stats", "context-stats",
      "active-tools", "tools", "agents", "skills", "files", "log"].forEach(id => $(id).textContent = "");
  }

  function row(label, value, depth = 0) {
    const indent = depth ? `style="padding-left:${Math.min(depth, 4) * 14}px"` : "";
    return `<div class="row" ${indent}><span>${esc(label)}</span><span>${esc(value)}</span></div>`;
  }

  async function loadSessions(query = "") {
    const requestId = ++sessionsRequest;
    let data;
    try {
      const response = await fetch(`/api/sessions?q=${encodeURIComponent(query)}`);
      if (!response.ok) throw new Error("sessions unavailable");
      data = await response.json();
    } catch (_) {
      if (requestId === sessionsRequest) $("connection").textContent = "API UNAVAILABLE · DATA NOT REFRESHING";
      return;
    }
    if (requestId !== sessionsRequest) return;
    sessions = data;
    const select = $("session");
    const previous = select.value;
    select.innerHTML = sessions.map(item =>
      `<option value="${esc(item.id)}">${esc(item.title)} · ${esc(item.id.slice(0, 8))}</option>`
    ).join("");
    if (sessions.some(item => item.id === previous)) select.value = previous;
    else if (sessions.length) select.selectedIndex = 0;
    if (select.value) await loadSession(select.value);
    else clearSession("No sessions found");
  }

  async function loadSession(id) {
    if (current && current.id !== id) clearSession("Loading session…");
    const requestId = ++detailRequest;
    let data;
    try {
      const response = await fetch(`/api/session/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("session unavailable");
      data = await response.json();
    } catch (_) {
      if (requestId === detailRequest) $("connection").textContent = "API UNAVAILABLE · DATA NOT REFRESHING";
      return;
    }
    if (requestId !== detailRequest || $("session").value !== id) return;
    current = data;
    $("connection").textContent = "LOCAL · READ ONLY";
    render();
  }

  function tokenChart(tokens) {
    // Input includes cached input; output includes reasoning. Use disjoint chart slices.
    const input = tokens.input_tokens;
    const cached = tokens.cached_input_tokens;
    const output = tokens.output_tokens;
    const slices = [];
    if (input != null && cached != null) {
      slices.push({ label: "Input excluding cache", value: Math.max(0, input - Math.min(input, cached)) });
      slices.push({ label: "Cache hit", value: Math.min(input, cached) });
    } else if (input != null) {
      slices.push({ label: "Input (cache split N/A)", value: input });
    }
    if (output != null) slices.push({ label: "Output", value: output });
    if (!slices.length) {
      $("token-chart").textContent = "Token data unavailable";
      $("token-legend").textContent = "";
      return;
    }
    const total = Math.max(1, slices.reduce((sum, item) => sum + item.value, 0));
    const mode = localStorage.getItem("chart-mode") || "bars";
    const chart = $("token-chart");
    chart.className = `chart ${mode}`;
    if (mode === "donut") {
      let start = 0;
      const stops = slices.map((item, index) => {
        const end = start + item.value / total * 100;
        const stop = `${colors[index]} ${start}% ${end}%`;
        start = end;
        return stop;
      });
      chart.innerHTML = `<div class="donut-ring" role="img" aria-label="Token input and output shares" style="background:conic-gradient(${stops.join(",")})"><span>${tokenFormat(tokens.total_tokens)}</span></div>`;
    } else if (mode === "stacked") {
      chart.innerHTML = `<div class="stacked-bar" role="img" aria-label="Input and output shares">${slices.map((item, index) =>
        `<span style="width:${item.value / total * 100}%;background:${colors[index]}" title="${esc(item.label)}: ${format(item.value)}"></span>`
      ).join("")}</div>`;
    } else {
      const maximum = Math.max(1, ...slices.map(item => item.value));
      chart.innerHTML = slices.map((item, index) => row(item.label, format(item.value)
      ) + `<div class="bar-track"><span style="width:${item.value / maximum * 100}%;background:${colors[index]}"></span></div>`).join("");
    }
    document.querySelectorAll("[data-chart]").forEach(button =>
      button.classList.toggle("active", button.dataset.chart === mode));
    $("token-legend").innerHTML = slices.map((item, index) => row(item.label, format(item.value), index === 2 ? 1 : 0)).join("")
      + row("Reasoning (included in output)", tokenFormat(tokens.reasoning_output_tokens), 1)
      + row("Total", tokenFormat(tokens.total_tokens));
  }

  function render() {
    if (!current) return;
    const session = current;
    $("session-id").textContent = session.id;
    $("title").textContent = session.title;
    const progress = session.loading_progress || { complete: true };
    const percent = progress.file_size ? Math.floor(progress.bytes_read / progress.file_size * 100) : 100;
    $("subtitle").textContent = `${session.cwd || "path unavailable"} · ${progress.complete ? "log loaded" : `loading ${percent}% — summary is provisional`} · refreshes every 3s`;
    if (progress.skipped_lines) $("subtitle").textContent += ` · skipped ${progress.skipped_lines} oversized lines; data may be incomplete`;
    $("now").textContent = session.active_at
      ? session.active_is_stale ? "LAST EVENT (STALE)" : "ACTIVE EVENT"
      : "NO ACTIVE TURN";
    $("freshness").textContent = `last event ${session.freshness_seconds}s ago`;
    $("model").textContent = `MODEL ${session.model || "—"} · EFFORT ${session.effort || "—"}`;

    const tokens = session.tokens || {};
    $("token-stats").innerHTML = [
      ["input_tokens", "INPUT"], ["cached_input_tokens", "CACHE HIT"],
      ["output_tokens", "OUTPUT"], ["reasoning_output_tokens", "REASONING · INCLUDED IN OUTPUT"],
      ["total_tokens", "TOTAL"]
    ].map(([key, label]) => `<div class="metric"><b>${tokenFormat(tokens[key])}</b><span>${label}</span></div>`).join("");
    tokenChart(tokens);

    $("time-stats").innerHTML = row("Completed turns", `${session.turns.completed} / ${session.turns.started}`)
      + row("Recorded turn duration", duration(session.turns.recorded_duration_seconds))
      + row("Time to first token", duration(session.turns.first_token_latency_seconds))
      + row("Tool elapsed · last 500 calls", duration(session.turns.tool_elapsed_seconds))
      + row("Observed wall time", duration(session.turns.wall_seconds))
      + row("Cost", "Unavailable");
    const modelRows = Object.entries(session.model_tokens || {}).map(([model, usage]) =>
      row(`Tokens · ${model}`, tokenFormat(usage.total_tokens))).join("");
    const limitRows = session.limits ? ["primary", "secondary"].map(name => {
      const limit = session.limits[name];
      return limit ? row(`Limit ${name}`, `${limit.used_percent == null ? "N/A" : `${limit.used_percent}%`} · ${limit.window_minutes == null ? "window N/A" : `${limit.window_minutes} min`} · reset ${limit.resets_at || limit.reset_at || "N/A"} · ${session.limits.observed_at}`) : "";
    }).join("") : row("Limits", "Unavailable");
    $("context-stats").innerHTML = row("Model / effort", `${session.model || "—"} / ${session.effort || "—"}`)
      + row("Token source", session.token_source || "N/A")
      + (session.token_discrepancy ? row("Snapshot difference", `${format(session.token_discrepancy.delta)} tokens`) : "")
      + row("Context window", session.context_window ? format(session.context_window) : "Unavailable")
      + row("Compactions", session.compactions == null ? "N/A" : session.compactions)
      + limitRows
      + modelRows;

    $("active-tools").innerHTML = session.active_tools.length
      ? session.active_tools.map(call => row(call.name, call.started)).join("")
      : row("No active tools", "according to the log");
    const stats = Object.entries(session.tool_stats || {});
    $("tools").innerHTML = stats.length
      ? stats.map(([name, value]) => row(name,
        `${value.count} calls · ${value.errors} errors · avg. ${duration(value.average_seconds)}`)).join("")
      : row("No tool calls found", "—");

    const agents = session.agents || [];
    $("agents").innerHTML = agents.length
      ? agents.map(agent => row(`${agent.agent_path || agent.agent_id || "agent"} ${agent.agent_id || ""}`,
        `${agent.model || ""} ${agent.effort || ""}${agent.parent_id ? ` · parent ${agent.parent_id}` : ""}`,
        agent.depth || 0)).join("")
      : row("No agent links observed", "—");
    $("skills").innerHTML = session.skills.length
      ? session.skills.map(skill => row(skill.name, skill.source)).join("")
      : row("No skills observed", "no evidence");
    $("files").innerHTML = session.files_changed.length
      ? session.files_changed.map(file => row(file, "FileChange observed")).join("")
      : row("No recognized file changes", "completeness is not guaranteed");
    $("log").innerHTML = session.log.map(entry =>
      `<span>${esc(entry.timestamp)}</span><span>${esc(entry.kind)}</span><span>${esc(entry.label)}</span>`
    ).join("");
  }

  async function downloadReport() {
    if (!current) return;
    const formatName = $("format").value;
    const scope = $("scope").value;
    const selected = current;
    let records = [];
    if (scope === "all") {
      const response = await fetch("/api/sessions");
      if (!response.ok) throw new Error("sessions unavailable");
      const allSessions = await response.json();
      records = allSessions;
    } else {
      records = [{ id: selected.id }];
    }
    const safeRecords = [];
    for (const item of records) {
      let record = null;
      let attempts = 0;
      do {
        if (!record || !record.loading_progress || !record.loading_progress.complete) {
          const response = await fetch(`/api/session/${encodeURIComponent(item.id)}`);
          if (!response.ok) throw new Error("session unavailable");
          record = await response.json();
        }
        if (record.loading_progress && !record.loading_progress.complete) {
          await new Promise(resolve => setTimeout(resolve, 20));
        } else break;
      } while (++attempts < 24);
      if (!record) continue;
      const value = { ...record };
      delete value.log;
      if (value.loading_progress && !value.loading_progress.complete) value.provisional = true;
      safeRecords.push(value);
    }
    const title = safeRecords.length === 1 ? safeRecords[0].title : `Codex activity · ${safeRecords.length} sessions`;
    let body;
    let mime;
    if (formatName === "json") {
      body = JSON.stringify(safeRecords, null, 2);
      mime = "application/json";
    } else if (formatName === "md") {
      const mdText = value => esc(value).replace(/[\\`*_[\]{}()#+.!|>-]/g, "\\$&").replace(/[\r\n]+/g, " ");
      const mdList = rows => rows.map(([label, value]) => `- ${mdText(label)}: ${mdText(value)}`).join("\n") || "No observed data";
      body = safeRecords.map(record => `# ${mdText(record.title)}\n\n`
        + `- Session: ${record.id}\n- Model: ${mdText(record.model || "N/A")} (${mdText(record.effort || "N/A")})\n`
        + (record.provisional ? "- WARNING: The log is incomplete; this summary is provisional.\n" : "")
        + (record.loading_progress?.skipped_lines ? `- Skipped ${record.loading_progress.skipped_lines} oversized lines; data may be incomplete.\n` : "")
        + `- Tokens: ${JSON.stringify(record.tokens)}\n- Context: ${record.context_window || "Unavailable"}\n`
        + `- Cost: Unavailable\n- Compactions: ${record.compactions ?? "N/A"}\n`
        + `- Token source: ${mdText(record.token_source || "N/A")}\n`
        + `- Log freshness: ${record.freshness_seconds}s\n`
        + `\n## Timing\n${mdList([
          ["Completed / started turns", `${record.turns.completed ?? "N/A"} / ${record.turns.started ?? "N/A"}`],
          ["Recorded turn duration", duration(record.turns.recorded_duration_seconds)],
          ["Time to first token", duration(record.turns.first_token_latency_seconds)],
          ["Tool elapsed · last 500 calls", duration(record.turns.tool_elapsed_seconds)],
          ["Observed wall time", duration(record.turns.wall_seconds)]])}\n`
        + `\n## Tools · last 500 calls\n${mdList(Object.entries(record.tool_stats || {}).map(([name, value]) =>
          [name, `${value.count} calls, ${value.errors} errors, avg. ${duration(value.average_seconds)}`]))}\n`
        + `\n## Active tools from log\n${mdList(record.active_tools.map(call => [call.name, call.started]))}\n`
        + `\n## Agents\n${mdList(record.agents.map(agent => [agent.agent_path || agent.agent_id,
          `${agent.agent_id} · parent ${agent.parent_id || "N/A"} · ${agent.model || "N/A"} / ${agent.effort || "N/A"}`]))}\n`
        + `\n## Limits\n${record.limits ? mdList(Object.entries(record.limits).map(([name, value]) =>
          [name, typeof value === "object" ? JSON.stringify(value) : value])) : "Unavailable"}\n`
        + `\n## Tokens by model\n${mdList(Object.entries(record.model_tokens || {}).map(([name, value]) => [name, JSON.stringify(value)]))}\n`
        + `\n## Skills\n${mdList(record.skills.map(skill => [skill.name, skill.source]))}\n`
        + `\n## Files from FileChange\n`
        + (record.files_changed.map(file => `- ${mdText(file)}`).join("\n") || "No observed files"))
        .join("\n\n");
      mime = "text/markdown";
    } else {
      body = `<!doctype html><meta charset="utf-8"><title>${esc(title)}</title>`
        + `<h1>${esc(title)}</h1><pre>${esc(JSON.stringify(safeRecords, null, 2))}</pre>`;
      mime = "text/html";
    }
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([body], { type: mime }));
    link.download = `codex-${scope}.${formatName === "md" ? "md" : formatName}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  $("search").addEventListener("input", event => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadSessions(event.target.value), 200);
  });
  $("session").addEventListener("change", event => loadSession(event.target.value));
  $("refresh").onclick = () => loadSessions($("search").value);
  $("show-all-panels").onclick = () => {
    document.querySelectorAll("[data-panel]").forEach(panel => {
      panel.hidden = false;
      localStorage.removeItem(`hidden:${panel.dataset.panel}`);
    });
  };
  $("print").onclick = () => window.print();
  $("export").onclick = () => $("export-dialog").showModal();
  $("download").onclick = () => downloadReport().catch(() => {
    $("connection").textContent = "EXPORT ERROR · REPORT NOT CREATED";
  });
  document.querySelectorAll("[data-chart]").forEach(button => button.onclick = () => {
    localStorage.setItem("chart-mode", button.dataset.chart);
    render();
  });
  document.querySelectorAll(".hide").forEach(button => button.onclick = () => {
    const panel = button.closest("[data-panel]");
    panel.hidden = !panel.hidden;
    localStorage.setItem(`hidden:${panel.dataset.panel}`, panel.hidden ? "1" : "0");
  });
  document.querySelectorAll("[data-panel]").forEach(panel =>
    panel.hidden = localStorage.getItem(`hidden:${panel.dataset.panel}`) === "1");
  document.addEventListener("keydown", event => {
    if (event.target.matches("input,select")) return;
    if (event.key.toLowerCase() === "r") loadSessions($("search").value);
    if (event.key.toLowerCase() === "v") $("show-all-panels").click();
    if (event.key === "/") { event.preventDefault(); $("search").focus(); }
    if (event.key === "Escape") document.querySelector("dialog").close();
  });

  loadSessions().catch(() => { $("connection").textContent = "API UNAVAILABLE"; });
  setInterval(() => $("session").value && loadSession($("session").value), 3000);
})();
