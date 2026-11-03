(() => {
  "use strict";

  const elements = Object.fromEntries([
    "setup-form", "form-error", "start-button", "primary-run-button", "refresh-button",
    "pause-button", "resume-button", "stop-button", "runtime-controls", "session-status",
    "session-status-text", "feed-indicator", "feed-label", "portfolio-chart", "chart-empty", "toast",
  ].map((id) => [id.replace(/-([a-z])/g, (_match, character) => character.toUpperCase()), document.getElementById(id)]));
  const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const percent = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let appState = null;
  let sessions = [];
  let loadedAt = Date.now();
  let chartRange = "24h";
  let toastTimer;
  let pollTimer;

  const euro = (value) => money.format(Number.isFinite(Number(value)) ? Number(value) : 0);
  const number = (value, digits = 6) => new Intl.NumberFormat("es-ES", { maximumFractionDigits: digits }).format(Number(value) || 0);
  const time = (timestamp, options = { hour: "2-digit", minute: "2-digit" }) => timestamp ? new Date(Number(timestamp) * 1000).toLocaleString("es-ES", options) : "—";

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
    const marketList = document.getElementById("market-list");
    marketList.replaceChildren();
    for (const quote of quotes) {
      const item = document.createElement("article");
      item.className = "market-item";
      const symbol = document.createElement("div");
      symbol.className = "market-symbol";
      symbol.textContent = quote.symbol;
      const name = document.createElement("div");
      name.className = "market-name";
      name.textContent = quote.name;
      const price = document.createElement("div");
      price.className = "market-price";
      price.textContent = euro(quote.price);
      const state = document.createElement("div");
      const quoteTime = quote.market_time || quote.fetched_at;
      const quoteAge = Math.max(0, Date.now() / 1000 - quoteTime);
      const fresh = quoteAge < 180;
      const stateKnown = quote.market_state !== "UNKNOWN";
      state.className = `market-state ${quote.error ? "error" : fresh && stateKnown && quote.market_state === "REGULAR" ? "open" : "closed"}`;
      const freshnessLabel = quoteAge < 60 ? "Ahora mismo" : `hace ${formatAge(quoteTime)}`;
      state.textContent = quote.error ? "Sin cotización" : `${marketStateLabel(quote.market_state)} · ${freshnessLabel}`;
      item.append(symbol, name, price, state);
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
    const active = experiment && ["running", "paused"].includes(experiment.status);
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
    document.getElementById("chart-current").textContent = euro(value);

    const history = experiment?.history || [];
    const firstValue = history[0]?.value || value;
    const change = value - firstValue;
    const changePercent = firstValue ? change / firstValue * 100 : 0;
    document.getElementById("chart-change").textContent = history.length ? `${change >= 0 ? "+" : ""}${euro(change)} (${percent.format(changePercent)}%)` : "Sin datos";
    document.getElementById("chart-change").className = `chart-change ${change > 0 ? "positive" : change < 0 ? "negative" : ""}`;
    elements.chartEmpty.hidden = history.length > 0;

    const formAvailable = !active;
    elements.setupForm.hidden = !formAvailable;
    elements.runtimeControls.hidden = formAvailable;
    elements.pauseButton.hidden = experiment?.status !== "running";
    elements.resumeButton.hidden = experiment?.status !== "paused";
    elements.primaryRunButton.textContent = active ? (experiment.status === "paused" ? "Reanudar simulación" : "Sesión en curso") : "Iniciar simulación +";
    elements.primaryRunButton.setAttribute("aria-label", elements.primaryRunButton.textContent);
    document.getElementById("session-id").textContent = experiment ? `INICIO ${time(experiment.started_at, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}` : "No hay sesión activa";
    const intervalLabels = { "1m": "1 min", "2m": "2 min", "5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 h", "1d": "1 día" };
    document.getElementById("runtime-strategy").textContent = experiment ? `Cruce ${experiment.config.fast_period}/${experiment.config.slow_period} · ${intervalLabels[experiment.config.interval] || experiment.config.interval}` : "Cruce de medias";
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
    for (const session of sessions) {
      const row = document.createElement("tr");
      const current = appState?.experiment;
      const finalValue = current?.id === session.id ? current.total_value : session.initial_balance + session.profit;
      row.append(
        makeCell(time(session.started_at, { day: "2-digit", month: "2-digit", year: "numeric" })),
        makeCell(session.config?.symbols.join(" · ") || "—"),
        makeCell(`${session.duration_days} días`),
        makeCell(euro(session.initial_balance)),
        makeCell(euro(finalValue)),
        makeCell(euro(session.profit), session.profit > 0 ? "positive" : session.profit < 0 ? "negative" : ""),
        makeCell(statuses[session.status] || session.status, "session-result"),
      );
      body.append(row);
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
    const padding = { top: 14, right: 8, bottom: 10, left: 64 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    let points = experiment?.history || [];
    const cutoff = chartRange === "24h" ? Date.now() / 1000 - 86400 : chartRange === "7d" ? Date.now() / 1000 - 604800 : 0;
    if (cutoff) points = points.filter((item) => item.timestamp >= cutoff);
    if (!points.length) return;
    const values = points.map((item) => item.value);
    let minimum = Math.min(...values);
    let maximum = Math.max(...values);
    const dataRange = maximum - minimum;
    const targetStep = Math.max(dataRange / 4, 1);
    const magnitude = 10 ** Math.floor(Math.log10(targetStep));
    const normalizedStep = targetStep / magnitude;
    const tickStep = (normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10) * magnitude;
    if (dataRange === 0) {
      const center = Math.round(values[0]);
      minimum = center - 2 * tickStep;
      maximum = center + 2 * tickStep;
    } else {
      minimum = Math.floor(minimum / tickStep) * tickStep;
      maximum = Math.ceil(maximum / tickStep) * tickStep;
    }
    const tickCount = Math.round((maximum - minimum) / tickStep);
    const tickFormat = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 });
    const x = (index) => padding.left + (points.length <= 1 ? plotWidth : index / (points.length - 1) * plotWidth);
    const y = (value) => padding.top + (maximum - value) / (maximum - minimum) * plotHeight;

    ctx.clearRect(0, 0, width, height);
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
    document.getElementById("chart-start-label").textContent = time(points[0].timestamp, { day: "2-digit", month: "short" }).toUpperCase();
    document.getElementById("chart-end-label").textContent = time(last.timestamp);
  }

  function render() {
    const experiment = appState?.experiment || null;
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
    const experiment = appState?.experiment;
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
      fast_period: Number(document.getElementById("fast-period").value),
      slow_period: Number(document.getElementById("slow-period").value),
      interval: document.getElementById("timeframe").value,
      fee_rate: Number(document.getElementById("fee-rate").value) / 100,
      allocation_percent: Number(document.getElementById("allocation-percent").value),
      duration_days: Number(document.getElementById("duration-days").value),
    };
    if (!selected.length || selected.length > 3) {
      elements.formError.textContent = "Selecciona entre una y tres acciones.";
      return;
    }
    if (config.slow_period <= config.fast_period) {
      elements.formError.textContent = "La media lenta debe ser mayor que la rápida.";
      return;
    }
    elements.startButton.disabled = true;
    try {
      await api("/api/sessions", { method: "POST", body: JSON.stringify(config) });
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
    elements.primaryRunButton.addEventListener("click", () => {
      if (appState?.experiment?.status === "paused") changeStatus("resume");
      else if (["running", "paused"].includes(appState?.experiment?.status)) document.getElementById("strategy").scrollIntoView({ behavior: "smooth" });
      else {
        document.getElementById("strategy").scrollIntoView({ behavior: "smooth" });
        document.getElementById("starting-balance").focus({ preventScroll: true });
      }
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
      window.requestAnimationFrame(() => drawChart(appState?.experiment));
    });
    document.querySelectorAll("[data-range]").forEach((button) => {
      button.addEventListener("click", () => {
        chartRange = button.dataset.range;
        document.querySelectorAll("[data-range]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", String(active));
        });
        drawChart(appState?.experiment);
      });
    });
    document.querySelectorAll(".nav-link").forEach((link) => {
      link.addEventListener("click", () => {
        document.querySelectorAll(".nav-link").forEach((item) => item.classList.remove("active"));
        link.classList.add("active");
      });
    });
    window.addEventListener("resize", () => drawChart(appState?.experiment));
    pollTimer = window.setInterval(refresh, 15_000);
    refresh();
  }

  init();
})();
