(() => {
  "use strict";

  const elements = Object.fromEntries([
    "setup-form", "form-error", "start-button", "primary-run-button", "refresh-button",
    "pause-button", "resume-button", "stop-button", "runtime-controls", "experiment-dialog",
    "close-experiment-button", "cancel-experiment-button", "experiment-tabs",
    "experiment-capacity", "experiment-empty", "session-status",
    "session-status-text", "feed-indicator", "feed-label", "portfolio-chart", "chart-empty",
    "chart-current", "chart-change",
    "chart-tooltip", "chart-tooltip-time", "chart-tooltip-value", "chart-tooltip-change",
    "chart-tooltip-cash", "chart-tooltip-invested", "chart-live", "chart-period-label",
    "chart-high", "chart-low", "chart-drawdown", "toast",
  ].map((id) => [id.replace(/-([a-z])/g, (_match, character) => character.toUpperCase()), document.getElementById(id)]));
  const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const percent = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let appState = null;
  let sessions = [];
  let selectedExperimentId = null;
  let viewedExperiment = null;
  let openQuoteHistorySymbol = null;
  let loadedAt = Date.now();
  let chartRange = "24h";
  let chartGeometry = null;
  let chartHoverIndex = null;
  let toastTimer;
  let pollTimer;

  const euro = (value) => money.format(Number.isFinite(Number(value)) ? Number(value) : 0);
  const number = (value, digits = 6) => new Intl.NumberFormat("es-ES", { maximumFractionDigits: digits }).format(Number(value) || 0);
  const time = (timestamp, options = { hour: "2-digit", minute: "2-digit" }) => timestamp ? new Date(Number(timestamp) * 1000).toLocaleString("es-ES", options) : "—";
  const chartRangeLabels = { "24h": "1 día", "7d": "1 semana", "30d": "1 mes", all: "Todo el experimento" };

  function getSelectedExperiment() {
    const active = appState?.experiments || [];
    return active.find((item) => item.id === selectedExperimentId)
      || (viewedExperiment?.id === selectedExperimentId ? viewedExperiment : null)
      || active[0]
      || appState?.experiment
      || null;
  }

  function renderExperimentTabs() {
    const active = [...(appState?.experiments || [])].sort((first, second) => second.return_percent - first.return_percent);
    const maxActive = appState?.max_active_experiments || 5;
    if (!active.some((item) => item.id === selectedExperimentId)
      && viewedExperiment?.id !== selectedExperimentId) {
      selectedExperimentId = active[0]?.id || null;
    }
    const selectedTabId = selectedExperimentId ? `experiment-tab-${selectedExperimentId}` : null;
    const selectedPanel = document.getElementById("selected-experiment-view");
    if (selectedPanel && selectedTabId) selectedPanel.setAttribute("aria-labelledby", selectedTabId);
    else selectedPanel?.removeAttribute("aria-labelledby");
    elements.experimentCapacity.textContent = `${active.length} / ${maxActive} ACTIVOS`;
    elements.experimentEmpty.hidden = active.length > 0;
    elements.experimentTabs.replaceChildren();
    elements.experimentTabs.hidden = active.length === 0;
    for (const experiment of active) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = `experiment-tab ${experiment.id === selectedExperimentId ? "active" : ""} ${experiment.error ? "has-error" : ""}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(experiment.id === selectedExperimentId));
      tab.setAttribute("aria-controls", "selected-experiment-view");
      tab.id = `experiment-tab-${experiment.id}`;
      tab.setAttribute("aria-label", `Ver ${experiment.config.symbols.join(", ")}, rentabilidad ${percent.format(experiment.return_percent)} por ciento`);
      tab.title = experiment.error || `${experiment.config.symbols.join(", ")} · ${experiment.config.interval}`;
      const identity = document.createElement("span");
      identity.className = "experiment-tab-identity";
      const symbols = document.createElement("span");
      symbols.className = "experiment-tab-symbols";
      symbols.textContent = experiment.config.symbols.join(" · ");
      const detail = document.createElement("small");
      const status = experiment.error ? "Error de datos" : experiment.status === "paused" ? "En pausa" : "En curso";
      detail.textContent = `${experiment.config.interval} · ${status}`;
      identity.append(symbols, detail);
      const result = document.createElement("strong");
      const returnPercent = Number(experiment.return_percent) || 0;
      result.textContent = `${returnPercent > 0 ? "+" : ""}${percent.format(returnPercent)}%`;
      result.className = returnPercent > 0 ? "positive" : returnPercent < 0 ? "negative" : "";
      tab.append(identity, result);
      tab.addEventListener("click", () => {
        selectedExperimentId = experiment.id;
        viewedExperiment = null;
        render();
      });
      tab.addEventListener("keydown", (event) => {
        const tabs = [...elements.experimentTabs.querySelectorAll('[role="tab"]')];
        const currentIndex = tabs.indexOf(tab);
        const nextIndex = event.key === "ArrowRight"
          ? (currentIndex + 1) % tabs.length
          : event.key === "ArrowLeft"
            ? (currentIndex - 1 + tabs.length) % tabs.length
            : event.key === "Home"
              ? 0
              : event.key === "End"
                ? tabs.length - 1
                : -1;
        if (nextIndex < 0) return;
        event.preventDefault();
        tabs[nextIndex].click();
        elements.experimentTabs.querySelectorAll('[role="tab"]')[nextIndex]?.focus();
      });
      elements.experimentTabs.append(tab);
    }
  }

  function getChartPoints(experiment) {
    const history = (experiment?.history || []).filter((item) => Number.isFinite(Number(item.timestamp)) && Number.isFinite(Number(item.value)));
    const now = Date.now() / 1000;
    const cutoff = chartRange === "24h" ? now - 86400 : chartRange === "7d" ? now - 604800 : chartRange === "30d" ? now - 2592000 : 0;
    return history.filter((item) => !cutoff || Number(item.timestamp) >= cutoff);
  }

  function renderChartSummary(experiment) {
    const value = Number(experiment?.total_value) || 0;
    const points = getChartPoints(experiment);
    const firstValue = points.length ? Number(points[0].value) : value;
    const change = value - firstValue;
    const changePercent = firstValue ? change / firstValue * 100 : 0;
    const hasPoints = points.length > 0;
    elements.chartCurrent.textContent = euro(value);
    elements.chartPeriodLabel.textContent = chartRangeLabels[chartRange] || "Periodo";
    elements.chartChange.textContent = hasPoints ? `${change >= 0 ? "+" : ""}${euro(change)} (${percent.format(changePercent)}%)` : "Sin datos";
    elements.chartChange.className = `chart-change ${change > 0 ? "positive" : change < 0 ? "negative" : ""}`;
    elements.chartEmpty.hidden = hasPoints;
    elements.chartEmpty.querySelector("span:last-child").textContent = (experiment?.history || []).length
      ? "No hay datos guardados para este periodo"
      : "El gráfico aparecerá al iniciar tu sesión";

    const values = points.map((item) => Number(item.value));
    elements.chartHigh.textContent = hasPoints ? euro(Math.max(...values)) : "—";
    elements.chartLow.textContent = hasPoints ? euro(Math.min(...values)) : "—";
    let peak = values[0] || 0;
    let maximumDrawdown = 0;
    for (const pointValue of values) {
      peak = Math.max(peak, pointValue);
      if (peak > 0) maximumDrawdown = Math.max(maximumDrawdown, (peak - pointValue) / peak * 100);
    }
    elements.chartDrawdown.textContent = hasPoints ? `${percent.format(maximumDrawdown)}%` : "—";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Error HTTP ${response.status}`);
    return payload;
  }

  function toast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 3600);
  }

  function setStatus(experiment) {
    const labels = {
      running: "En curso",
      paused: "En pausa",
      complete: "Completada",
      stopped: "Finalizada",
    };
    const status = experiment?.status || "stopped";
    elements.sessionStatus.className = `session-status status-${status}`;
    elements.sessionStatusText.textContent = labels[status] || "Sin iniciar";
  }

  function renderMarket() {
    const quotes = appState?.quotes || [];
    const watchedSymbols = new Set(getSelectedExperiment()?.config.symbols || []);
    const watchedQuotes = quotes.filter((quote) => watchedSymbols.has(quote.symbol));
    if (openQuoteHistorySymbol && !watchedSymbols.has(openQuoteHistorySymbol)) {
      openQuoteHistorySymbol = null;
    }
    const marketList = document.getElementById("market-list");
    marketList.replaceChildren();
    for (const quote of watchedQuotes) {
      const item = document.createElement("article");
      item.className = "market-item";
      const symbol = document.createElement("div");
      symbol.className = "market-symbol";
      symbol.textContent = quote.symbol;
      const name = document.createElement("div");
      name.className = "market-name";
      name.textContent = quote.name;
      item.dataset.symbol = quote.symbol;
      item.classList.toggle("history-open", openQuoteHistorySymbol === quote.symbol);
      const priceButton = document.createElement("button");
      priceButton.type = "button";
      priceButton.className = "market-price-trigger";
      priceButton.setAttribute("aria-label", `Ver últimas consultas de ${quote.symbol}`);
      priceButton.setAttribute("aria-controls", `quote-history-${quote.symbol}`);
      priceButton.setAttribute("aria-expanded", String(openQuoteHistorySymbol === quote.symbol));
      priceButton.title = `Ver historial reciente de ${quote.symbol}`;
      const price = document.createElement("div");
      price.className = "market-price";
      price.textContent = euro(quote.price);
      const historyHint = document.createElement("span");
      historyHint.className = "market-history-hint";
      historyHint.textContent = "HISTORIAL";
      priceButton.append(price, historyHint);
      priceButton.addEventListener("click", () => {
        openQuoteHistorySymbol = openQuoteHistorySymbol === quote.symbol ? null : quote.symbol;
        marketList.querySelectorAll(".market-item").forEach((marketItem) => {
          const expanded = marketItem.dataset.symbol === openQuoteHistorySymbol;
          marketItem.classList.toggle("history-open", expanded);
          marketItem.querySelector(".market-price-trigger")?.setAttribute("aria-expanded", String(expanded));
        });
      });
      const historyPanel = document.createElement("div");
      historyPanel.className = "quote-history";
      historyPanel.id = `quote-history-${quote.symbol}`;
      historyPanel.setAttribute("role", "region");
      historyPanel.setAttribute("aria-label", `Historial de consultas de ${quote.symbol}`);
      const historyHeader = document.createElement("div");
      historyHeader.className = "quote-history-header";
      historyHeader.textContent = "Últimas consultas";
      historyPanel.append(historyHeader);
      const historyList = document.createElement("div");
      historyList.className = "quote-history-list";
      for (const point of quote.history || []) {
        const row = document.createElement("div");
        row.className = "quote-history-row";
        const timestamp = document.createElement("time");
        timestamp.dateTime = new Date(Number(point.fetched_at) * 1000).toISOString();
        timestamp.textContent = time(point.fetched_at, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        const historyPrice = document.createElement("span");
        historyPrice.textContent = euro(point.price);
        row.append(timestamp, historyPrice);
        historyList.append(row);
      }
      if (!historyList.childElementCount) {
        const emptyHistory = document.createElement("div");
        emptyHistory.className = "quote-history-empty";
        emptyHistory.textContent = "Aún no hay consultas guardadas.";
        historyList.append(emptyHistory);
      }
      historyPanel.append(historyList);
      const state = document.createElement("div");
      const quoteTime = quote.market_time || quote.fetched_at;
      const quoteAge = Math.max(0, Date.now() / 1000 - quoteTime);
      const fresh = quoteAge < 180;
      const stateKnown = quote.market_state !== "UNKNOWN";
      state.className = `market-state ${quote.error ? "error" : fresh && stateKnown && quote.market_state === "REGULAR" ? "open" : "closed"}`;
      const freshnessLabel = quoteAge < 60 ? "Ahora mismo" : `hace ${formatAge(quoteTime)}`;
      state.textContent = quote.error ? "Sin cotización" : `${marketStateLabel(quote.market_state)} · ${freshnessLabel}`;
      item.append(symbol, name, priceButton, state, historyPanel);
      marketList.append(item);
    }

    const indicator = elements.feedIndicator;
    const lastSuccess = appState?.market?.last_success_at;
    const marketTimes = quotes.map((quote) => Number(quote.market_time)).filter(Number.isFinite);
    const latestMarketTime = marketTimes.length ? Math.max(...marketTimes) : null;
    const ageSeconds = latestMarketTime ? Math.max(0, Date.now() / 1000 - latestMarketTime) : Infinity;
    if (appState?.market?.last_error && !quotes.length) {
      indicator.dataset.mode = "error";
      elements.feedLabel.textContent = "Sin datos de mercado";
    } else if (ageSeconds < 180) {
      indicator.dataset.mode = "live";
      elements.feedLabel.textContent = ageSeconds < 60 ? "Ahora mismo" : "Precios recientes";
    } else if (latestMarketTime) {
      indicator.dataset.mode = "mixed";
      elements.feedLabel.textContent = `Últimos precios hace ${formatAge(latestMarketTime)}`;
    } else if (lastSuccess) {
      indicator.dataset.mode = "mixed";
      elements.feedLabel.textContent = `Última consulta hace ${formatAge(lastSuccess)}`;
    } else {
      indicator.dataset.mode = "waiting";
      elements.feedLabel.textContent = "Esperando cotizaciones";
    }
    document.getElementById("market-updated").textContent = latestMarketTime ? `Último precio ${time(latestMarketTime)}` : lastSuccess ? `Última consulta ${time(lastSuccess)}` : appState?.market?.last_error ? "Error al consultar el proveedor" : "Esperando primera cotización";
  }

  function marketStateLabel(value) {
    return ({ REGULAR: "Mercado abierto", PRE: "Preapertura", POST: "Posmercado", CLOSED: "Mercado cerrado" })[value] || "Última cotización";
  }

  function formatAge(timestamp) {
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    if (seconds < 60) return `${seconds} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
    return `${Math.floor(seconds / 3600)} h`;
  }

  function renderMetrics(experiment) {
    const activeCount = appState?.experiments?.length || 0;
    const maxActive = appState?.max_active_experiments || 5;
    const canCreate = activeCount < maxActive;
    const value = experiment ? experiment.total_value : 0;
    const profit = experiment ? experiment.profit : 0;
    const elapsed = experiment ? experiment.elapsed_seconds + (experiment.status === "running" ? (Date.now() - loadedAt) / 1000 : 0) : 0;
    const sign = profit > 0 ? "positive" : profit < 0 ? "negative" : "";
    document.getElementById("portfolio-value").textContent = euro(value);
    document.getElementById("pnl-value").textContent = euro(profit);
    document.getElementById("pnl-value").className = `metric-value ${sign}`;
    document.getElementById("return-value").textContent = `${profit > 0 ? "+" : ""}${percent.format(experiment?.return_percent || 0)}%`;
    document.getElementById("return-value").className = `metric-value ${sign}`;
    document.getElementById("elapsed-value").textContent = formatClock(elapsed);
    document.getElementById("portfolio-foot").textContent = experiment ? `Efectivo disponible: ${euro(experiment.cash)}` : "Inicia un experimento para comenzar";
    document.getElementById("pnl-foot").textContent = experiment ? `Saldo inicial: ${euro(experiment.initial_balance)}` : "Frente al saldo inicial";
    document.getElementById("return-foot").textContent = experiment ? `Comisiones acumuladas: ${euro(experiment.total_fees)}` : "Resultado simulado";
    document.getElementById("duration-foot").textContent = experiment ? `Objetivo: ${experiment.duration_days} días` : "Duración objetivo: 30 días";
    renderChartSummary(experiment);

    elements.setupForm.hidden = !canCreate;
    elements.startButton.disabled = !canCreate;
    elements.runtimeControls.hidden = !experiment;
    elements.pauseButton.hidden = experiment?.status !== "running";
    elements.resumeButton.hidden = experiment?.status !== "paused";
    elements.stopButton.hidden = !["running", "paused"].includes(experiment?.status);
    elements.primaryRunButton.textContent = canCreate ? "Nuevo experimento +" : "Límite de experimentos";
    elements.primaryRunButton.disabled = !canCreate;
    elements.primaryRunButton.setAttribute("aria-label", elements.primaryRunButton.textContent);
    document.getElementById("session-id").textContent = experiment ? `INICIO ${time(experiment.started_at, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}` : "No hay sesión activa";
    const intervalLabels = { "1m": "1 min", "2m": "2 min", "5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 h", "1d": "1 día" };
    const config = experiment?.config;
    document.getElementById("runtime-strategy").textContent = !config
      ? "Grid escalonado"
      : config.strategy === "momentum_target"
        ? `Impulso · ${config.entry_trigger_type === "price" ? `€${number(config.entry_price_eur, 2)}` : `+${percent.format(config.entry_change_percent)}%/${config.entry_lookback_candles} velas`} · objetivo neto +${percent.format(config.profit_target_percent)}%`
        : `Grid ${percent.format(config.grid_step_percent ?? 2)}/${percent.format(config.take_profit_percent ?? 2)}% · ${config.grid_tranches ?? 4} tramos · Stop ${percent.format(config.stop_loss_percent ?? 5)}%`;
    document.getElementById("runtime-assets").textContent = experiment?.config.symbols.join(" · ") || "";
    document.getElementById("runtime-hint").textContent = experiment?.error || "El worker del servidor sigue activo si cierras esta página. El equipo y el contenedor deben permanecer encendidos.";
    setStatus(experiment);
  }

  function makeCell(value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = value;
    if (className) cell.className = className;
    return cell;
  }

  function renderPositions(experiment) {
    const body = document.getElementById("holdings-body");
    body.replaceChildren();
    const positions = experiment?.positions || [];
    document.getElementById("position-count").textContent = `${positions.length} ${positions.length === 1 ? "ACTIVA" : "ACTIVAS"}`;
    document.getElementById("holdings-empty").hidden = positions.length > 0;
    for (const position of positions) {
      const row = document.createElement("tr");
      const symbol = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = "asset-pill";
      pill.textContent = position.symbol;
      symbol.append(pill);
      row.append(symbol,
        makeCell(number(position.quantity)),
        makeCell(euro(position.average_price)),
        makeCell(euro(position.price)),
        makeCell(euro(position.value)),
        makeCell(euro(position.pnl), position.pnl > 0 ? "positive" : position.pnl < 0 ? "negative" : ""));
      body.append(row);
    }
  }

  function renderTrades(experiment) {
    const body = document.getElementById("trades-body");
    body.replaceChildren();
    const trades = experiment?.trades || [];
    document.getElementById("trade-count").textContent = `${trades.length} ${trades.length === 1 ? "OPERACIÓN" : "OPERACIONES"}`;
    document.getElementById("trades-empty").hidden = trades.length > 0;
    for (const trade of trades) {
      const row = document.createElement("tr");
      const symbol = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = "asset-pill";
      pill.textContent = trade.symbol;
      symbol.append(pill);
      row.append(symbol,
        makeCell(trade.side === "buy" ? "Compra" : "Venta", `trade-side ${trade.side}`),
        makeCell(euro(trade.price)),
        makeCell(euro(trade.fee)),
        makeCell(time(trade.timestamp)));
      body.append(row);
    }
  }

  function renderSessions() {
    const body = document.getElementById("experiments-body");
    body.replaceChildren();
    document.getElementById("experiment-count").textContent = `${sessions.length} ${sessions.length === 1 ? "SESIÓN" : "SESIONES"}`;
    document.getElementById("experiments-empty").hidden = sessions.length > 0;
    const statuses = { running: "En curso", paused: "En pausa", complete: "Completada", stopped: "Finalizada" };
    for (const session of [...sessions].sort((first, second) => second.return_percent - first.return_percent)) {
      const row = document.createElement("tr");
      const current = (appState?.experiments || []).find((item) => item.id === session.id)
        || (viewedExperiment?.id === session.id ? viewedExperiment : null);
      const finalValue = current?.total_value ?? session.initial_balance + session.profit;
      const returnPercent = Number(session.return_percent) || 0;
      const viewCell = document.createElement("td");
      const viewButton = document.createElement("button");
      viewButton.type = "button";
      viewButton.className = "session-open-button";
      viewButton.textContent = "Ver";
      viewButton.setAttribute("aria-label", `Ver experimento ${session.config?.symbols.join(", ") || ""}`);
      viewButton.addEventListener("click", () => viewSession(session.id));
      viewCell.append(viewButton);
      row.append(
        makeCell(time(session.started_at, { day: "2-digit", month: "2-digit", year: "numeric" })),
        makeCell(session.config?.symbols.join(" · ") || "—"),
        makeCell(`${session.duration_days} días`),
        makeCell(euro(session.initial_balance)),
        makeCell(euro(finalValue)),
        makeCell(`${returnPercent > 0 ? "+" : ""}${percent.format(returnPercent)}%`, returnPercent > 0 ? "positive" : returnPercent < 0 ? "negative" : ""),
        makeCell(euro(session.profit), session.profit > 0 ? "positive" : session.profit < 0 ? "negative" : ""),
        makeCell(statuses[session.status] || session.status, "session-result"),
        viewCell,
      );
      body.append(row);
    }
  }

  async function viewSession(experimentId) {
    selectedExperimentId = experimentId;
    const active = (appState?.experiments || []).find((item) => item.id === experimentId);
    if (active) {
      viewedExperiment = null;
      render();
      return;
    }
    try {
      viewedExperiment = await api(`/api/sessions/${experimentId}`);
      render();
    } catch (error) {
      toast(error.message);
    }
  }

  function drawChart(experiment) {
    const canvas = elements.portfolioChart;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const width = rect.width;
    const height = rect.height;
    const padding = { top: 12, right: 10, bottom: 10, left: 72 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const points = getChartPoints(experiment);
    ctx.clearRect(0, 0, width, height);
    chartGeometry = null;
    if (!points.length) {
      elements.chartTooltip.hidden = true;
      document.getElementById("chart-start-label").textContent = "—";
      document.getElementById("chart-end-label").textContent = "—";
      return;
    }

    const values = points.map((item) => Number(item.value));
    const initialBalance = Number(experiment?.initial_balance);
    const domainValues = Number.isFinite(initialBalance) ? [...values, initialBalance] : values;
    const rawMinimum = Math.min(...domainValues);
    const rawMaximum = Math.max(...domainValues);
    const dataRange = rawMaximum - rawMinimum;
    const domainPadding = dataRange ? dataRange * 0.08 : Math.max(Math.abs(rawMaximum) * 0.01, 1);
    const targetStep = Math.max((dataRange + domainPadding * 2) / 4, 1);
    const magnitude = 10 ** Math.floor(Math.log10(targetStep));
    const normalizedStep = targetStep / magnitude;
    const tickStep = (normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10) * magnitude;
    const minimum = Math.floor((rawMinimum - domainPadding) / tickStep) * tickStep;
    const maximum = Math.ceil((rawMaximum + domainPadding) / tickStep) * tickStep;
    const tickCount = Math.round((maximum - minimum) / tickStep);
    const tickFormat = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
    const x = (index) => padding.left + (points.length <= 1 ? plotWidth : index / (points.length - 1) * plotWidth);
    const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;

    ctx.font = "9px 'IBM Plex Mono', monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let index = 0; index <= tickCount; index += 1) {
      const label = maximum - index * tickStep;
      const yPosition = y(label);
      ctx.beginPath();
      ctx.moveTo(padding.left, yPosition);
      ctx.lineTo(width - padding.right, yPosition);
      ctx.strokeStyle = "#1b3827";
      ctx.stroke();
      ctx.fillStyle = "#829987";
      ctx.fillText(tickFormat.format(label), padding.left - 8, yPosition);
    }

    if (Number.isFinite(initialBalance)) {
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.moveTo(padding.left, y(initialBalance));
      ctx.lineTo(width - padding.right, y(initialBalance));
      ctx.strokeStyle = "#c7ff61";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    const gradient = ctx.createLinearGradient(0, padding.top, 0, height - padding.bottom);
    gradient.addColorStop(0, "rgba(69, 239, 145, .17)");
    gradient.addColorStop(1, "rgba(18, 139, 91, 0)");
    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(x(index), y(point.value)) : ctx.moveTo(x(index), y(point.value)));
    ctx.lineTo(x(points.length - 1), height - padding.bottom);
    ctx.lineTo(x(0), height - padding.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(x(index), y(point.value)) : ctx.moveTo(x(index), y(point.value)));
    ctx.strokeStyle = "#45ef91";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
    const last = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(x(points.length - 1), y(last.value), 3.5, 0, Math.PI * 2);
    ctx.fillStyle = "#c7ff61";
    ctx.fill();
    ctx.strokeStyle = "#45ef91";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    chartGeometry = { points, padding, plotWidth, plotHeight, minimum, maximum, x, y };
    if (chartHoverIndex !== null && chartHoverIndex < points.length) {
      const activePoint = points[chartHoverIndex];
      ctx.beginPath();
      ctx.setLineDash([3, 4]);
      ctx.moveTo(x(chartHoverIndex), padding.top);
      ctx.lineTo(x(chartHoverIndex), height - padding.bottom);
      ctx.strokeStyle = "#819987";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(x(chartHoverIndex), y(Number(activePoint.value)), 5, 0, Math.PI * 2);
      ctx.fillStyle = "#07100b";
      ctx.fill();
      ctx.strokeStyle = "#c7ff61";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    document.getElementById("chart-start-label").textContent = time(points[0].timestamp, { day: "2-digit", month: "short" }).toUpperCase();
    document.getElementById("chart-end-label").textContent = time(last.timestamp);
  }

  function showChartTooltip(index, announce = false) {
    if (!chartGeometry || index < 0 || index >= chartGeometry.points.length) return;
    const point = chartGeometry.points[index];
    const firstValue = Number(chartGeometry.points[0].value);
    const value = Number(point.value);
    const cash = Number(point.cash) || 0;
    const change = value - firstValue;
    const changePercent = firstValue ? change / firstValue * 100 : 0;
    elements.chartTooltipTime.textContent = time(point.timestamp, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    elements.chartTooltipValue.textContent = euro(value);
    elements.chartTooltipChange.textContent = `${change >= 0 ? "+" : ""}${euro(change)} · ${changePercent >= 0 ? "+" : ""}${percent.format(changePercent)}%`;
    elements.chartTooltipChange.className = `chart-tooltip-change ${change > 0 ? "positive" : change < 0 ? "negative" : ""}`;
    elements.chartTooltipCash.textContent = euro(cash);
    elements.chartTooltipInvested.textContent = euro(value - cash);

    elements.chartTooltip.hidden = false;
    if (announce) {
      elements.chartLive.textContent = `${time(point.timestamp)}. Cartera ${euro(value)}. Efectivo ${euro(cash)}. Invertido ${euro(value - cash)}.`;
    }
  }

  function moveChartSelection(event) {
    if (!chartGeometry || !chartGeometry.points.length) return;
    const rect = elements.portfolioChart.getBoundingClientRect();
    const localX = event.clientX - rect.left;
    const fraction = Math.max(0, Math.min(1, (localX - chartGeometry.padding.left) / chartGeometry.plotWidth));
    chartHoverIndex = Math.round(fraction * (chartGeometry.points.length - 1));
    drawChart(getSelectedExperiment());
    showChartTooltip(chartHoverIndex);
  }

  function render() {
    renderExperimentTabs();
    const experiment = getSelectedExperiment();
    renderMarket();
    renderMetrics(experiment);
    renderPositions(experiment);
    renderTrades(experiment);
    renderSessions();
    drawChart(experiment);
  }

  async function refresh() {
    try {
      const [nextState, nextSessions] = await Promise.all([api("/api/state"), api("/api/sessions")]);
      appState = nextState;
      sessions = nextSessions;
      loadedAt = Date.now();
      render();
    } catch (error) {
      elements.feedIndicator.dataset.mode = "error";
      elements.feedLabel.textContent = "Servidor no disponible";
      toast(error.message || "No se pudo consultar el servidor.");
    }
  }

  async function changeStatus(action) {
    const experiment = getSelectedExperiment();
    if (!experiment) return;
    try {
      appState = await api(`/api/sessions/${experiment.id}/${action}`, { method: "POST" });
      await refresh();
      toast(({ pause: "Simulación en pausa.", resume: "Simulación reanudada.", stop: "Experimento finalizado y guardado." })[action]);
    } catch (error) {
      toast(error.message);
    }
  }

  async function startExperiment(event) {
    event.preventDefault();
    elements.formError.textContent = "";
    const selected = [...document.querySelectorAll('input[name="asset"]:checked')].map((input) => input.value);
    const config = {
      starting_balance: Number(document.getElementById("starting-balance").value),
      symbols: selected,
      strategy: document.getElementById("strategy-mode").value,
      fast_period: Number(document.getElementById("fast-period").value),
      slow_period: Number(document.getElementById("slow-period").value),
      interval: document.getElementById("timeframe").value,
      fee_rate: Number(document.getElementById("fee-rate").value) / 100,
      allocation_percent: Number(document.getElementById("allocation-percent").value),
      stop_loss_percent: Number(document.getElementById("stop-loss-percent").value),
      grid_step_percent: Number(document.getElementById("grid-step-percent").value),
      take_profit_percent: Number(document.getElementById("take-profit-percent").value),
      grid_tranches: Number(document.getElementById("grid-tranches").value),
      entry_trigger_type: document.getElementById("entry-trigger-type").value,
      entry_change_percent: Number(document.getElementById("entry-change-percent").value),
      entry_lookback_candles: Number(document.getElementById("entry-lookback-candles").value),
      entry_price_eur: document.getElementById("entry-price-eur").value
        ? Number(document.getElementById("entry-price-eur").value)
        : null,
      profit_target_percent: Number(document.getElementById("profit-target-percent").value),
      duration_days: Number(document.getElementById("duration-days").value),
    };
    if (!selected.length || selected.length > 3) {
      elements.formError.textContent = "Selecciona entre una y tres acciones.";
      return;
    }
    if (config.strategy === "moving_average_grid" && config.slow_period <= config.fast_period) {
      elements.formError.textContent = "La media lenta debe ser mayor que la rápida.";
      return;
    }
    elements.startButton.disabled = true;
    try {
      const started = await api("/api/sessions", { method: "POST", body: JSON.stringify(config) });
      selectedExperimentId = started.experiment.id;
      viewedExperiment = null;
      elements.experimentDialog.close();
      await refresh();
      toast(`Experimento iniciado para ${config.duration_days} días. El worker opera en el servidor.`);
    } catch (error) {
      elements.formError.textContent = error.message;
    } finally {
      elements.startButton.disabled = false;
    }
  }

  function formatClock(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return days ? `${days}d ${[hours, minutes, remainder].map((value) => String(value).padStart(2, "0")).join(":")}` : [hours, minutes, remainder].map((value) => String(value).padStart(2, "0")).join(":");
  }

  function init() {
    elements.setupForm.addEventListener("submit", startExperiment);
    const strategyMode = document.getElementById("strategy-mode");
    const entryTriggerType = document.getElementById("entry-trigger-type");
    const updateStrategyFields = () => {
      const selectedStrategy = strategyMode.value;
      document.querySelectorAll(".strategy-only").forEach((group) => {
        const visible = group.dataset.strategy === selectedStrategy;
        group.hidden = !visible;
        group.querySelectorAll("input, select").forEach((input) => {
          input.required = visible && (!input.closest("[data-entry-trigger]") || input.closest("[data-entry-trigger]").dataset.entryTrigger === entryTriggerType.value);
        });
      });
      document.querySelectorAll("[data-entry-trigger]").forEach((group) => {
        const visible = selectedStrategy === "momentum_target" && group.dataset.entryTrigger === entryTriggerType.value;
        group.hidden = !visible;
        group.querySelector("input").required = visible;
      });
    };
    strategyMode.addEventListener("change", updateStrategyFields);
    entryTriggerType.addEventListener("change", updateStrategyFields);
    updateStrategyFields();
    elements.closeExperimentButton.addEventListener("click", () => elements.experimentDialog.close());
    elements.cancelExperimentButton.addEventListener("click", () => elements.experimentDialog.close());
    elements.experimentDialog.addEventListener("click", (event) => {
      if (event.target === elements.experimentDialog) elements.experimentDialog.close();
    });
    document.addEventListener("click", (event) => {
      if (openQuoteHistorySymbol && !event.target.closest(".market-item")) {
        openQuoteHistorySymbol = null;
        document.querySelectorAll(".market-item.history-open").forEach((item) => {
          item.classList.remove("history-open");
          item.querySelector(".market-price-trigger")?.setAttribute("aria-expanded", "false");
        });
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && elements.experimentDialog.open) {
        elements.experimentDialog.close();
        elements.formError.textContent = "";
        return;
      }
      if (event.key === "Escape" && openQuoteHistorySymbol) {
        openQuoteHistorySymbol = null;
        document.querySelectorAll(".market-item.history-open").forEach((item) => {
          item.classList.remove("history-open");
          item.querySelector(".market-price-trigger")?.setAttribute("aria-expanded", "false");
        });
      }
    });
    elements.primaryRunButton.addEventListener("click", () => {
      if ((appState?.experiments?.length || 0) >= (appState?.max_active_experiments || 5)) {
        toast("Ya alcanzaste el máximo de experimentos activos.");
        return;
      }
      elements.formError.textContent = "";
      elements.experimentDialog.showModal();
      document.getElementById("starting-balance").focus({ preventScroll: true });
    });
    elements.pauseButton.addEventListener("click", () => changeStatus("pause"));
    elements.resumeButton.addEventListener("click", () => changeStatus("resume"));
    elements.stopButton.addEventListener("click", () => {
      if (window.confirm("¿Finalizar el experimento? El resultado y las posiciones quedarán guardados.")) changeStatus("stop");
    });
    elements.refreshButton.addEventListener("click", refresh);
    document.getElementById("chart-expand").addEventListener("click", (event) => {
      const panel = document.querySelector(".chart-panel");
      panel.classList.toggle("chart-expanded");
      const expanded = panel.classList.contains("chart-expanded");
      event.currentTarget.textContent = expanded ? "−" : "+";
      event.currentTarget.title = expanded ? "Cerrar gráfico ampliado" : "Ampliar gráfico";
      window.requestAnimationFrame(() => drawChart(getSelectedExperiment()));
    });
    elements.portfolioChart.addEventListener("pointermove", moveChartSelection);
    elements.portfolioChart.addEventListener("pointerleave", () => {
      if (document.activeElement === elements.portfolioChart) return;
      chartHoverIndex = null;
      elements.chartTooltip.hidden = true;
      drawChart(getSelectedExperiment());
    });
    elements.portfolioChart.addEventListener("focus", () => {
      if (!chartGeometry?.points.length) return;
      chartHoverIndex = chartGeometry.points.length - 1;
      drawChart(getSelectedExperiment());
      showChartTooltip(chartHoverIndex, true);
    });
    elements.portfolioChart.addEventListener("blur", () => {
      chartHoverIndex = null;
      elements.chartTooltip.hidden = true;
      drawChart(getSelectedExperiment());
    });
    elements.portfolioChart.addEventListener("keydown", (event) => {
      if (!chartGeometry?.points.length) return;
      let nextIndex = chartHoverIndex ?? chartGeometry.points.length - 1;
      if (event.key === "ArrowLeft") nextIndex -= 1;
      else if (event.key === "ArrowRight") nextIndex += 1;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = chartGeometry.points.length - 1;
      else if (event.key === "Escape") {
        chartHoverIndex = null;
        elements.chartTooltip.hidden = true;
        drawChart(getSelectedExperiment());
        return;
      } else return;
      event.preventDefault();
      chartHoverIndex = Math.max(0, Math.min(chartGeometry.points.length - 1, nextIndex));
      drawChart(getSelectedExperiment());
      showChartTooltip(chartHoverIndex, true);
    });
    document.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => {
        chartRange = button.dataset.range;
        chartHoverIndex = null;
        elements.chartTooltip.hidden = true;
        document.querySelectorAll("[data-range]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", String(active));
        });
        renderChartSummary(getSelectedExperiment());
        drawChart(getSelectedExperiment());
      });
    });
    const navLinks = [...document.querySelectorAll(".nav-link")];
    navLinks.forEach((link) => {
      link.addEventListener("click", () => {
        navLinks.forEach((item) => item.classList.remove("active"));
        link.classList.add("active");
      });
    });
    const navSections = navLinks
      .map((link) => document.querySelector(link.getAttribute("href")))
      .filter(Boolean);
    if ("IntersectionObserver" in window) {
      const navObserver = new IntersectionObserver((entries) => {
        const visible = entries.filter((entry) => entry.isIntersecting).sort((first, second) => second.intersectionRatio - first.intersectionRatio)[0];
        if (!visible) return;
        navLinks.forEach((link) => link.classList.toggle("active", link.hash === `#${visible.target.id}`));
      }, { rootMargin: "-18% 0px -68% 0px", threshold: [0, 0.2, 0.5] });
      navSections.forEach((section) => navObserver.observe(section));
    }
    window.addEventListener("resize", () => drawChart(getSelectedExperiment()));
    pollTimer = window.setInterval(refresh, 15_000);
    refresh();
  }

  init();
})();
