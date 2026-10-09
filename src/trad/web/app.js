(() => {
  "use strict";

  let latestState = null;
  let busy = false;
  const $ = (selector) => document.querySelector(selector);

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const value = (item, fallback = "—") => item === null || item === undefined || item === "" ? fallback : escapeHtml(item);
  const money = (item) => value(item, "0.00");
  const signed = (item) => {
    if (item === null || item === undefined) return "—";
    const text = String(item);
    return `<span class="${text.startsWith("-") ? "negative" : "positive"}">${escapeHtml(text)}</span>`;
  };
  const isoForInput = (date) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };
  const isoFromInput = (text) => {
    if (!text) return "";
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  };

  function setDefaultMarketTimes() {
    const now = new Date();
    const open = new Date(now.getTime() - 60 * 1000);
    $("#market-timestamp").value = isoForInput(open);
    $("#market-close-time").value = isoForInput(now);
    $("#market-received-at").value = isoForInput(now);
  }

  function healthBadge(health, badgeId, reasonId) {
    const badge = $(badgeId);
    const reason = $(reasonId);
    if (!health) {
      badge.className = "badge badge-danger";
      badge.textContent = "UNAVAILABLE";
      reason.textContent = "No health response.";
      return;
    }
    const safe = health.status === "safe" && health.allow_new_positions;
    badge.className = `badge ${safe ? "badge-success" : "badge-danger"}`;
    badge.textContent = String(health.status || "unknown").replaceAll("_", " ").toUpperCase();
    reason.textContent = health.reason || "No reason provided.";
  }

  function renderWallet(targetId, wallet) {
    const target = $(targetId);
    if (!wallet || !wallet.length) {
      target.innerHTML = '<div class="empty">No recorded wallet balances.</div>';
      return;
    }
    target.innerHTML = `<div class="wallet-row header"><span>Asset</span><span>Available</span><span>Reserved</span><span>Total</span></div>${wallet.map((balance) => `
      <div class="wallet-row"><span><strong>${value(balance.asset)}</strong></span><span>${money(balance.available)}</span><span>${money(balance.reserved)}</span><span>${money(balance.total)}</span></div>`).join("")}`;
  }

  function renderSpot(state) {
    renderWallet("#spot-wallet", state.wallet);
    healthBadge(state.market_data, "#spot-health-badge", "#spot-health-reason");
    const valuation = state.valuation;
    $("#spot-valuation").innerHTML = valuation ? `
      <strong>Portfolio valuation in ${value(valuation.valuation_asset)}</strong><br>
      <span class="${valuation.total_value === null ? "negative" : "positive"}">${valuation.total_value === null ? "Incomplete — unpriced: " + value((valuation.unpriced_assets || []).join(", ")) : money(valuation.total_value)}</span>
      <span class="muted"> · ${valuation.total_value === null ? "not treated as a total" : "last recorded explicit price"}</span>` : '<span class="empty">Valuation unavailable until a safe explicit price is recorded.</span>';
    const reconciliation = state.reconciliation || {};
    const reconciliationNode = $("#spot-reconciliation");
    reconciliationNode.className = `reconciliation ${reconciliation.is_consistent ? "good" : "bad"}`;
    reconciliationNode.innerHTML = reconciliation.is_consistent ? "✓ Spot wallet and ledger reconcile." : `<strong>Spot reconciliation requires attention</strong><br>${value((reconciliation.differences || []).join("; "))}`;
    renderSpotOrders(state.orders || []);
  }

  function renderFutures(state) {
    renderWallet("#futures-wallet", state.wallet);
    healthBadge(state.market_data, "#futures-health-badge", "#futures-health-reason");
    const storage = state.persistence || {};
    $("#futures-storage").textContent = storage.durable ? "durable local SQLite snapshots + recovery" : "in-memory only";
    const valuation = state.valuation;
    $("#futures-valuation").innerHTML = valuation ? `
      <strong>Futures equity in ${value(valuation.valuation_asset)}</strong><br>
      <span class="positive">${value(valuation.account_equity)}</span>
      <span class="muted"> · wallet ${value(valuation.wallet_total)} · unrealized ${signed(valuation.unrealized_pnl)} · ${valuation.mark_is_current ? "safe recorded mark" : "last mark is not current; treat equity as an estimate"}</span>` : '<span class="empty">Futures valuation unavailable.</span>';
    renderFuturesOrders(state.orders || []);
    const position = (state.positions || [])[0];
    $("#futures-position").innerHTML = position ? `
      <div class="position-grid">
        <div class="metric"><small>Direction</small><strong>${value(position.side)}</strong></div>
        <div class="metric"><small>Quantity</small><strong>${value(position.quantity)}</strong></div>
        <div class="metric"><small>Leverage</small><strong>${value(position.leverage)}x</strong></div>
        <div class="metric"><small>Entry</small><strong>${value(position.entry_price)}</strong></div>
        <div class="metric"><small>Last explicit mark</small><strong>${value(position.mark_price)}</strong></div>
        <div class="metric"><small>Notional</small><strong>${value(position.notional)}</strong></div>
        <div class="metric"><small>Margin</small><strong>${value(position.margin)}</strong></div>
        <div class="metric"><small>Maintenance</small><strong>${value(position.maintenance_margin)}</strong></div>
        <div class="metric"><small>Realized P&amp;L</small><strong>${signed(position.realized_pnl)}</strong></div>
        <div class="metric"><small>Unrealized P&amp;L</small><strong>${signed(position.unrealized_pnl)}</strong></div>
        <div class="metric"><small>Equity</small><strong>${value(position.equity)}</strong></div>
      </div>` : '<div class="empty">No open Futures position. A position is never inferred from missing market data.</div>';
    const reconciliation = state.reconciliation || {};
    const reconciliationNode = $("#futures-reconciliation");
    reconciliationNode.className = `reconciliation ${reconciliation.is_consistent ? "good" : "bad"}`;
    reconciliationNode.innerHTML = reconciliation.is_consistent ? "✓ Wallet, ledger, reservations, and positions reconcile." : `<strong>Reconciliation requires attention</strong><br>${value((reconciliation.issues || []).join("; "))}`;
  }

  function orderStatus(status) {
    const normalized = String(status || "unknown");
    return `<span class="status-text status-${escapeHtml(normalized)}">${escapeHtml(normalized.replaceAll("_", " "))}</span>`;
  }

  function renderSpotOrders(orders) {
    const target = $("#spot-orders");
    if (!orders.length) {
      target.innerHTML = '<div class="empty">No Spot orders yet.</div>';
      return;
    }
    target.innerHTML = `<table><thead><tr><th>Id / client key</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Filled / fee</th><th>Actions</th></tr></thead><tbody>${orders.slice().reverse().map((order) => `
      <tr><td><strong>${value(order.order_id)}</strong><br><span class="muted">${value(order.client_order_id)}</span></td><td>${value(order.side)}</td><td class="num">${value(order.quantity)}</td><td class="num">${value(order.price)}</td><td>${orderStatus(order.status)}${order.rejection_reason ? `<br><span class="muted">${value(order.rejection_reason)}</span>` : ""}</td><td class="num">${value(order.filled_quantity)}<br><span class="muted">fee ${value(order.fee_paid, "0")}</span></td><td>${order.status === "accepted" || order.status === "partially_filled" ? `<button class="table-action" data-fill-domain="spot" data-order-id="${escapeHtml(order.order_id)}">Fill form</button> <button class="table-action" data-cancel-domain="spot" data-order-id="${escapeHtml(order.order_id)}">Cancel</button>` : "—"}</td></tr>`).join("")}</tbody></table>`;
  }

  function renderFuturesOrders(orders) {
    const target = $("#futures-orders");
    if (!orders.length) {
      target.innerHTML = '<div class="empty">No Futures orders yet.</div>';
      return;
    }
    target.innerHTML = `<table><thead><tr><th>Id / client key</th><th>Action / side</th><th>Qty</th><th>Price</th><th>Status</th><th>Filled / fee</th><th>Actions</th></tr></thead><tbody>${orders.slice().reverse().map((order) => `
      <tr><td><strong>${value(order.order_id)}</strong><br><span class="muted">${value(order.client_order_id)}</span></td><td>${value(order.action)} / ${value(order.position_side)}</td><td class="num">${value(order.quantity)}</td><td class="num">${value(order.price)}</td><td>${orderStatus(order.status)}${order.rejection_reason ? `<br><span class="muted">${value(order.rejection_reason)}</span>` : ""}</td><td class="num">${value(order.filled_quantity)}<br><span class="muted">fee ${value(order.fee_paid, "0")}</span></td><td>${order.status === "accepted" || order.status === "partially_filled" ? `<button class="table-action" data-fill-domain="futures" data-order-id="${escapeHtml(order.order_id)}">Fill form</button> <button class="table-action" data-cancel-domain="futures" data-order-id="${escapeHtml(order.order_id)}">Cancel</button>` : "—"}</td></tr>`).join("")}</tbody></table>`;
  }

  function renderHistory(state) {
    const fills = [...(state.spot.fills || []).map((item) => ({...item, domain: "Spot"})), ...(state.futures.fills || []).map((item) => ({...item, domain: "Futures"}))].slice(-8).reverse();
    const funding = (state.futures.funding || []).slice(-8).reverse();
    const liquidations = (state.futures.liquidations || []).slice(-8).reverse();
    const audits = (state.futures.audit_events || []).slice(-8).reverse();
    const ledger = (state.spot.ledger || []).slice(-8).reverse();
    const list = (items, render, empty) => items.length ? `<ul>${items.map(render).join("")}</ul>` : `<div class="empty">${empty}</div>`;
    $("#history").innerHTML = `
      <div class="history-box"><h3>Spot ledger</h3>${list(ledger, (item) => `<li><strong>${value(item.entry_type)}</strong> · ${value(item.entry_id)}</li>`, "No Spot ledger entries yet.")}</div>
      <div class="history-box"><h3>Fills</h3>${list(fills, (item) => `<li><strong>${value(item.domain)}</strong> ${value(item.fill_id)} · ${value(item.quantity)} @ ${value(item.price)}</li>`, "No fills yet.")}</div>
      <div class="history-box"><h3>Funding</h3>${list(funding, (item) => `<li><strong>${value(item.amount)}</strong> at rate ${value(item.rate)} · ${value(item.payment_id)}</li>`, "No funding payments yet.")}</div>
      <div class="history-box"><h3>Liquidations</h3>${list(liquidations, (item) => `<li><strong>${value(item.position_side)}</strong> at ${value(item.mark_price)} · shortfall ${value(item.shortfall)}</li>`, "No liquidation events recorded.")}</div>
      <div class="history-box"><h3>Futures audit</h3>${list(audits, (item) => `<li><strong>${value(item.event_type)}</strong> · ${value(item.event_id)}</li>`, "No Futures audit events yet.")}</div>`;
  }

  function renderAutomation(automation) {
    const state = automation || {};
    const current = String(state.state || "unavailable");
    const badge = $("#automation-state");
    badge.className = `badge ${current === "running" ? "badge-success" : current === "blocked" ? "badge-danger" : "badge-warning"}`;
    badge.textContent = current.replaceAll("_", " ").toUpperCase();
    $("#automation-reason").textContent = state.blocked_reason || (current === "running" ? "Paper loop is active and waits for completed validated candles." : "Evaluate accepted strategies before starting the paper loop.");
    const selected = state.selected || {};
    const selectionNode = $("#automation-selected");
    const domains = ["spot", "futures"];
    selectionNode.innerHTML = domains.map((domain) => {
      const name = selected[domain];
      return `<div class="selection-card"><strong>${escapeHtml(domain.toUpperCase())}</strong><br><span class="${name ? "selected" : "negative"}">${name ? `selected: ${value(name)}` : "NO ACCEPTED STRATEGY — BLOCKED"}</span></div>`;
    }).join("");
    const rows = [];
    Object.entries(state.selections || {}).forEach(([domain, selection]) => {
      (selection.results || []).forEach((result) => {
        const metrics = result.validation || {};
        rows.push(`<tr><td>${value(domain)}</td><td><strong>${value(result.strategy_display_name || result.strategy_name)}</strong><br><span class="muted">${value(result.strategy_name)}</span></td><td class="${result.accepted ? "selected" : "strategy-failed"}">${value(result.status)}</td><td>${result.accepted ? "accepted" : "failed"}</td><td class="num">${value(metrics.return_pct)}</td><td class="num">${value(metrics.max_drawdown)}</td><td class="num">${value(metrics.trades, "0")}</td><td>${value((result.failure_reasons || []).join("; "))}</td></tr>`);
      });
    });
    $("#strategy-results").innerHTML = rows.length ? `<table><thead><tr><th>Domain</th><th>Strategy</th><th>Status</th><th>Eligible</th><th>Validation return</th><th>Drawdown</th><th>Trades</th><th>Failure reason</th></tr></thead><tbody>${rows.join("")}</tbody></table>` : '<div class="empty">No strategy evaluation has been persisted yet.</div>';
    const decisions = state.recent_decisions || [];
    $("#automation-decisions").innerHTML = decisions.length ? `<ul>${decisions.map((item) => `<li><strong>${value(item.domain)}</strong> ${value(item.action)} · ${value(item.strategy_name)}<br><span class="muted">${value(item.reason)}</span></li>`).join("")}</ul>` : '<div class="empty">No decisions yet.</div>';
    const errors = [...(state.recent_errors || []).map((item) => ({...item, kind: "error"})), ...(state.recent_recoveries || []).map((item) => ({...item, kind: "recovery"}))].slice(-12).reverse();
    $("#automation-errors").innerHTML = errors.length ? `<ul>${errors.map((item) => `<li><strong>${value(item.kind === "error" ? item.category : item.event_type)}</strong> · ${value(item.message || (item.payload || {}).reason)}</li>`).join("")}</ul>` : '<div class="empty">No runner errors or recovery events yet.</div>';
  }

  function render(state) {
    latestState = state;
    renderAutomation(state.automation);
    $("#connection-status").textContent = "Connected · state is authoritative backend data";
    $("#generated-at").textContent = `last refresh ${value(state.generated_at)}`;
    $("#global-alert").textContent = state.warning || "Paper trading only. No real orders are submitted.";
    renderSpot(state.spot);
    renderFutures(state.futures);
    renderHistory(state);
  }

  function showToast(message, kind = "success") {
    const toast = $("#toast");
    toast.textContent = message;
    toast.className = `toast show ${kind}`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { toast.className = "toast"; }, 4500);
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {cache: "no-store", ...options, headers: {"Content-Type": "application/json", ...(options.headers || {})}});
    let body;
    try { body = await response.json(); } catch (_error) { throw new Error(`Server returned ${response.status} without JSON`); }
    if (!response.ok || body.ok === false) {
      throw new Error(body.error?.message || `Request failed with ${response.status}`);
    }
    return body.data;
  }

  async function refresh() {
    try {
      const state = await request("/api/state");
      render(state);
    } catch (error) {
      $("#connection-status").textContent = "Unavailable";
      showToast(error.message, "error");
    }
  }

  function formObject(form) {
    const data = {};
    new FormData(form).forEach((item, key) => { data[key] = item; });
    return data;
  }

  async function submitForm(form, path, transform = (data) => data, successMessage = "Operation confirmed by backend.") {
    if (busy) return;
    busy = true;
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      const payload = transform(formObject(form));
      await request(path, {method: "POST", body: JSON.stringify(payload)});
      showToast(successMessage, "success");
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
      await refresh();
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }

  function fillForm(domain, orderId) {
    const form = domain === "spot" ? $("#spot-fill-form") : $("#futures-fill-form");
    form.elements.order_id.value = orderId;
    form.scrollIntoView({behavior: "smooth", block: "center"});
    form.elements.quantity.focus();
  }

  async function cancelOrder(domain, orderId) {
    if (busy || !window.confirm(`Cancel the remaining ${domain} order ${orderId}?`)) return;
    busy = true;
    try {
      await request(`/api/${domain}/orders/${encodeURIComponent(orderId)}/cancel`, {method: "POST", body: "{}"});
      showToast("Cancellation confirmed by backend.", "success");
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
      await refresh();
    } finally { busy = false; }
  }

  async function automationRequest(path, payload, message) {
    if (busy) return;
    busy = true;
    try {
      await request(path, {method: "POST", body: JSON.stringify(payload || {})});
      showToast(message, "success");
      await refresh();
    } catch (error) {
      showToast(error.message, "error");
      await refresh();
    } finally { busy = false; }
  }

  $("#automation-evaluate").addEventListener("click", () => automationRequest("/api/automation/evaluate", {domain: "both"}, "All registered strategies evaluated; no orders were started."));
  $("#automation-start").addEventListener("click", () => {
    if (window.confirm("Start paper automation? Only strategies that passed validation may open paper positions.")) automationRequest("/api/automation/start", {confirm: true}, "Paper automation start recorded by the backend.");
  });
  $("#automation-pause").addEventListener("click", () => automationRequest("/api/automation/pause", {}, "Paper automation paused safely."));
  $("#automation-resume").addEventListener("click", () => {
    if (window.confirm("Resume paper automation? Fresh validated data is still required.")) automationRequest("/api/automation/resume", {confirm: true}, "Paper automation resumed by the backend.");
  });
  $("#automation-stop").addEventListener("click", () => {
    if (window.confirm("Stop paper automation? Existing paper positions are not silently reset.")) automationRequest("/api/automation/stop", {confirm: true}, "Paper automation stopped safely.");
  });

  $("#market-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    submitForm(form, "/api/market-data", (data) => ({...data, timestamp: isoFromInput(data.timestamp), close_time: isoFromInput(data.close_time), received_at: isoFromInput(data.received_at)}), "Validated candle recorded; safety status refreshed.");
  });

  $("#reset-safety").addEventListener("click", async () => {
    if (busy || !window.confirm("Reset both safety monitors? Fresh validated market data will be required again.")) return;
    busy = true;
    try { await request("/api/market-data/reset", {method: "POST", body: JSON.stringify({confirm: true})}); showToast("Safety monitors reset to no-data.", "success"); await refresh(); }
    catch (error) { showToast(error.message, "error"); }
    finally { busy = false; }
  });

  $("#spot-order-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget, "/api/spot/orders", undefined, "Spot order accepted or recorded as a backend rejection.");
  });
  $("#spot-fill-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    submitForm(form, `/api/spot/orders/${encodeURIComponent(form.elements.order_id.value)}/fill`, (data) => ({quantity: data.quantity, price: data.price, fill_id: data.fill_id}), "Spot fill confirmed by backend.");
  });
  $("#futures-order-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget, "/api/futures/orders", (data) => {
      if (data.action === "reduce") delete data.leverage;
      return data;
    }, "Futures order accepted or recorded as a backend rejection.");
  });
  $("#futures-fill-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    submitForm(form, `/api/futures/orders/${encodeURIComponent(form.elements.order_id.value)}/fill`, (data) => ({quantity: data.quantity, price: data.price, fill_id: data.fill_id}), "Futures fill confirmed by backend.");
  });
  $("#futures-mark-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!window.confirm("Apply this explicit mark? The backend may trigger simulated liquidation.")) return;
    submitForm(event.currentTarget, "/api/futures/mark", undefined, "Explicit Futures mark confirmed by backend.");
  });
  $("#futures-funding-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.currentTarget, "/api/futures/funding", undefined, "Funding payment confirmed by backend.");
  });
  $("#futures-action").addEventListener("change", (event) => {
    $("#leverage-field").style.opacity = event.currentTarget.value === "reduce" ? ".48" : "1";
    $("#leverage-field input").disabled = event.currentTarget.value === "reduce";
  });
  document.addEventListener("click", (event) => {
    const fill = event.target.closest("[data-fill-domain]");
    const cancel = event.target.closest("[data-cancel-domain]");
    if (fill) fillForm(fill.dataset.fillDomain, fill.dataset.orderId);
    if (cancel) cancelOrder(cancel.dataset.cancelDomain, cancel.dataset.orderId);
  });

  setDefaultMarketTimes();
  refresh();
  window.setInterval(refresh, 5000);
})();
