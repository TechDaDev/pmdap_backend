/* PMDAP Railway server monitor dashboard.
 * Polls the admin data endpoint (Redis only) every 3s while the tab is visible,
 * aborts in-flight requests on new cycles / hide, and renders Chart.js charts
 * with numeric current+peak values (never colour-only).
 */
(function () {
  "use strict";

  var cfg = window.PMDAP_MONITOR || { dataUrl: "", pollMs: 3000 };
  var windowSeconds = 900;

  // DOM elements are resolved lazily inside functions: the script runs from
  // <head> (extrahead) before the <body> exists.
  function el(id) { return document.getElementById(id); }

  var charts = {}; // service -> { cpu, memdisk, net }
  var controller = null;
  var timer = null;
  var lastData = null;

  var METRICS = {
    CPU_USAGE: { label: "CPU", unit: "vCPU" },
    MEMORY_USAGE_GB: { label: "Memory", unit: "GB" },
    DISK_USAGE_GB: { label: "Disk", unit: "GB" },
    NETWORK_RX_GB: { label: "Network RX", unit: "MB/s" },
    NETWORK_TX_GB: { label: "Network TX", unit: "MB/s" }
  };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function buildServiceCards(serviceNames) {
    var servicesEl = el("monitor-services");
    servicesEl.innerHTML = "";
    charts = {};
    serviceNames.forEach(function (name) {
      var card = document.createElement("div");
      card.className = "ops-service-card";
      card.id = "svc-" + name.replace(/[^a-zA-Z0-9_-]/g, "_");
      // Summary element ids must be unique per service (charts already are).
      function curId(metric) { return "cur-" + name.replace(/[^a-zA-Z0-9_-]/g, "_") + "-" + metric; }
      card.innerHTML =
        '<h2>' + escapeHtml(name) + '</h2>' +
        '<div class="ops-service-summary">' +
          '<div class="ops-summary-item"><span class="ops-current" id="' + curId("CPU_USAGE") + '" data-metric="CPU_USAGE">-</span> <span class="ops-label">vCPU now</span></div>' +
          '<div class="ops-summary-item"><span class="ops-current" id="' + curId("MEMORY_USAGE_GB") + '" data-metric="MEMORY_USAGE_GB">-</span> <span class="ops-label">GB now</span></div>' +
          '<div class="ops-summary-item"><span class="ops-current" id="' + curId("DISK_USAGE_GB") + '" data-metric="DISK_USAGE_GB">-</span> <span class="ops-label">GB now</span></div>' +
          '<div class="ops-summary-item"><span class="ops-current" id="' + curId("NETWORK_TX_GB") + '" data-metric="NETWORK_TX_GB">-</span> <span class="ops-label">MB/s out</span></div>' +
          '<div class="ops-summary-item"><span class="ops-current" id="' + curId("NETWORK_RX_GB") + '" data-metric="NETWORK_RX_GB">-</span> <span class="ops-label">MB/s in</span></div>' +
        '</div>' +
        '<div class="ops-chart"><canvas id="chart-cpu-' + escapeHtml(name) + '" aria-label="CPU usage for ' + escapeHtml(name) + '"></canvas></div>' +
        '<div class="ops-chart"><canvas id="chart-mem-' + escapeHtml(name) + '" aria-label="Memory and disk usage for ' + escapeHtml(name) + '"></canvas></div>' +
        '<div class="ops-chart"><canvas id="chart-net-' + escapeHtml(name) + '" aria-label="Network throughput for ' + escapeHtml(name) + '"></canvas></div>';
      servicesEl.appendChild(card);
      charts[name] = {
        cpu: new Chart(document.getElementById("chart-cpu-" + name), {
          type: "line",
          data: { labels: [], datasets: [{ label: "CPU (vCPU)", data: [], borderColor: "#417690", backgroundColor: "rgba(65,118,144,0.1)", tension: 0.2 }] },
          options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: true, title: { display: true, text: "Time" } }, y: { beginAtZero: true } } }
        }),
        memdisk: new Chart(document.getElementById("chart-mem-" + name), {
          type: "line",
          data: { labels: [], datasets: [
            { label: "Memory (GB)", data: [], borderColor: "#79aec8", backgroundColor: "rgba(121,174,200,0.1)", tension: 0.2 },
            { label: "Disk (GB)", data: [], borderColor: "#ba2121", backgroundColor: "rgba(186,33,33,0.1)", tension: 0.2 }
          ] },
          options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: true, title: { display: true, text: "Time" } }, y: { beginAtZero: true } } }
        }),
        net: new Chart(document.getElementById("chart-net-" + name), {
          type: "line",
          data: { labels: [], datasets: [
            { label: "RX (MB/s)", data: [], borderColor: "#417690", backgroundColor: "rgba(65,118,144,0.1)", tension: 0.2 },
            { label: "TX (MB/s)", data: [], borderColor: "#f5dd5d", backgroundColor: "rgba(245,221,93,0.1)", tension: 0.2 }
          ] },
          options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: true, title: { display: true, text: "Time" } }, y: { beginAtZero: true } } }
        })
      };
    });
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "-";
    return value.toFixed(digits === undefined ? 2 : digits);
  }

  function timeLabel(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  }

  function seriesWithin(points, now) {
    var cutoff = now - windowSeconds;
    return (points || []).filter(function (p) { return p[0] >= cutoff; });
  }

  function lastValue(points) {
    if (!points || !points.length) return null;
    return points[points.length - 1][1];
  }

  function maxValue(points) {
    if (!points || !points.length) return null;
    return Math.max.apply(null, points.map(function (p) { return p[1]; }));
  }

  /* Cumulative GB counters -> MB/s rate using successive samples. */
  function toRate(points) {
    var out = [];
    for (var i = 1; i < points.length; i++) {
      var dt = points[i][0] - points[i - 1][0];
      if (dt <= 0) continue;
      var gbPerSec = (points[i][1] - points[i - 1][1]) / dt;
      out.push([points[i][0], Math.max(0, gbPerSec * 1024)]); // GB/s -> MB/s
    }
    return out;
  }

  function updateChart(chart, labels, datasetIndex, values) {
    chart.data.labels = labels;
    chart.data.datasets[datasetIndex].data = values;
    chart.update();
  }

  function updateService(name, series, now) {
    var c = charts[name];
    if (!c) return;
    var cpu = seriesWithin(series.CPU_USAGE, now);
    var mem = seriesWithin(series.MEMORY_USAGE_GB, now);
    var disk = seriesWithin(series.DISK_USAGE_GB, now);
    var rx = toRate(seriesWithin(series.NETWORK_RX_GB, now));
    var tx = toRate(seriesWithin(series.NETWORK_TX_GB, now));

    updateChart(c.cpu, cpu.map(function (p) { return timeLabel(p[0]); }), 0, cpu.map(function (p) { return p[1]; }));
    updateChart(c.memdisk, mem.map(function (p) { return timeLabel(p[0]); }), 0, mem.map(function (p) { return p[1]; }));
    updateChart(c.memdisk, mem.map(function (p) { return timeLabel(p[0]); }), 1, disk.map(function (p) { return p[1]; }));
    updateChart(c.net, rx.map(function (p) { return timeLabel(p[0]); }), 0, rx.map(function (p) { return p[1]; }));
    updateChart(c.net, tx.map(function (p) { return timeLabel(p[0]); }), 1, tx.map(function (p) { return p[1]; }));

    var ids = {
      CPU_USAGE: fmt(lastValue(cpu), 3) + " vCPU (peak " + fmt(maxValue(cpu), 3) + ")",
      MEMORY_USAGE_GB: fmt(lastValue(mem), 2) + " GB (peak " + fmt(maxValue(mem), 2) + ")",
      DISK_USAGE_GB: fmt(lastValue(disk), 2) + " GB (peak " + fmt(maxValue(disk), 2) + ")",
      NETWORK_TX_GB: fmt(lastValue(tx), 2) + " MB/s (peak " + fmt(maxValue(tx), 2) + ")",
      NETWORK_RX_GB: fmt(lastValue(rx), 2) + " MB/s (peak " + fmt(maxValue(rx), 2) + ")"
    };
    var prefix = "cur-" + name.replace(/[^a-zA-Z0-9_-]/g, "_") + "-";
    Object.keys(ids).forEach(function (metric) {
      var summaryEl = document.getElementById(prefix + metric);
      if (summaryEl) summaryEl.textContent = ids[metric];
    });
  }

  function staleSeconds(data) {
    return data.now - (data.last_ok_at || data.updated_at || 0);
  }

  function render(data) {
    var statusTextEl = el("monitor-status-text");
    var status = data.status || "STALE";
    var threshold = Math.max(3 * (data.sample_seconds || 5), 30);
    var stale = status !== "OK" || staleSeconds(data) > threshold;

    var badge = stale ? '<span class="ops-stale">STALE</span>' : '<span class="ops-ok">LIVE</span>';
    var statusText = "Collector status: " + status + " &middot; last update " + (data.updated_at ? timeLabel(data.updated_at) : "never");
    if (stale) statusText += " &middot; data is stale";
    if (statusTextEl) statusTextEl.innerHTML = badge + " " + statusText;

    var names = Object.keys(data.services || {});
    if (charts[names[0]] === undefined || (lastData && JSON.stringify(Object.keys(lastData.services || {})) !== JSON.stringify(names))) {
      buildServiceCards(names);
    }
    names.forEach(function (name) {
      updateService(name, data.services[name] || {}, data.now);
    });
    lastData = data;
  }

  function fetchData() {
    if (controller) controller.abort();
    controller = new AbortController();
    fetch(cfg.dataUrl, { credentials: "same-origin", signal: controller.signal, headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        var statusTextEl = el("monitor-status-text");
        if (statusTextEl) {
          statusTextEl.innerHTML = '<span class="ops-stale">OFFLINE</span> Cannot reach monitor data endpoint: ' + escapeHtml(String(err));
        }
      })
      .finally(function () { controller = null; });
  }

  function visibilityChanged() {
    if (document.visibilityState === "visible") {
      if (!timer) timer = setInterval(fetchData, cfg.pollMs);
      fetchData();
    } else {
      if (timer) { clearInterval(timer); timer = null; }
      if (controller) { controller.abort(); controller = null; }
    }
  }

  function init() {
    var windowEl = document.getElementById("monitor-window");
    if (windowEl) {
      windowEl.addEventListener("click", function (e) {
        var btn = e.target.closest(".ops-window-btn");
        if (!btn) return;
        windowSeconds = parseInt(btn.getAttribute("data-window"), 10);
        document.querySelectorAll(".ops-window-btn").forEach(function (b) { b.classList.remove("is-active"); });
        btn.classList.add("is-active");
        if (lastData) render(lastData);
      });
    }
    document.addEventListener("visibilitychange", visibilityChanged);
    visibilityChanged();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
