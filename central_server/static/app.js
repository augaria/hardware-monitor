'use strict';

const POLL_INTERVAL_MS = 5000;

// Per-machine UI state (preserved across re-renders since the grid rebuilds every poll).
const ArraysExpanded = new Set();   // machine names whose Arrays section is expanded

// ── Color helpers ─────────────────────────────────────────────────────────────

function colorClass(value, warnAt, critAt) {
  if (value === null || value === undefined) return 'na';
  if (value >= critAt)  return 'red';
  if (value >= warnAt)  return 'yellow';
  return 'green';
}

const COLOR_RULES = {
  cpu_usage:        [50, 75],
  cpu_temp:         [60, 80],
  memory_usage:     [50, 75],
  motherboard_temp: [50, 65],
  gpu_usage:        [50, 75],
  gpu_temp:         [60, 80],
  gpu_memory_usage: [50, 75],
  disk_temp:        [45, 55],
  disk_usage:       [70, 90],
};

function metricColor(key, val) {
  const rule = COLOR_RULES[key];
  return rule ? colorClass(val, rule[0], rule[1]) : 'green';
}

const COLOR_RANK = { na: 0, green: 1, yellow: 2, red: 3 };
function worseColor(a, b) {
  return COLOR_RANK[a] >= COLOR_RANK[b] ? a : b;
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

function hasValue(v) {
  return v !== null && v !== undefined;
}

function makeMetricCell(label, value, colorKey, unit, showBar, suffix) {
  const cls  = metricColor(colorKey, value);
  const disp = hasValue(value)
    ? `${parseFloat(value).toFixed(1)}${unit || ''}`
    : null;

  const barHtml = (showBar && disp !== null)
    ? `<div class="bar-wrap"><div class="bar-fill ${cls}" style="width:${Math.min(value,100)}%"></div></div>`
    : '';

  const suffixHtml = suffix ? `<span class="metric-suffix">${suffix}</span>` : '';

  return `
    <div class="metric-cell">
      <span class="metric-label">${label}</span>
      <span class="metric-value-row">
        <span class="metric-value ${disp !== null ? cls : 'na'}">${disp !== null ? disp : '—'}</span>
        ${suffixHtml}
      </span>
      ${barHtml}
    </div>`;
}

function makeHwBadges(m) {
  if (m.is_vm) {
    const label = m.virt_type ? `VM ${m.virt_type.toUpperCase()}` : 'VM';
    return `<span class="hw-badge hw-badge-vm">${label}</span>`;
  }
  const badges = [];
  if (m.cpu_model) {
    const cores = hasValue(m.cpu_cores) ? ` · ${m.cpu_cores} cores` : '';
    badges.push(`<span class="hw-badge hw-badge-cpu">CPU ${m.cpu_model}${cores}</span>`);
  }
  if (hasValue(m.memory_total_gb)) {
    badges.push(`<span class="hw-badge hw-badge-ram">RAM ${m.memory_total_gb} GB</span>`);
  }
  if (m.gpu_model) {
    badges.push(`<span class="hw-badge hw-badge-gpu">GPU ${m.gpu_model}</span>`);
  }
  return badges.join('');
}

function makeOsBadge(m) {
  if (!m.os_name) return '';
  return `<span class="hw-badge hw-badge-os">${m.os_name}</span>`;
}

function makeTypeBadge(m) {
  if (m.machine_type !== 'nas' && m.machine_type !== 'server') return '';
  const label = m.machine_type.toUpperCase();
  return `<span class="type-badge type-${m.machine_type}">${label}</span>`;
}

function formatCapacity(gb) {
  if (!hasValue(gb)) return '';
  if (gb >= 1000) return `${(gb / 1024).toFixed(1)} TB`;
  if (gb >= 10)   return `${Math.round(gb)} GB`;
  return `${gb.toFixed(1)} GB`;
}

// ── Mounts table — second table beneath arrays (same visual style) ──

function makeMountsTable(arrays) {
  const mounted = arrays.filter(a => a.mount);
  if (!mounted.length) return '';
  const head = `<thead><tr>
    <th class="col-name">Array</th>
    <th class="col-mount">Mount Path</th>
  </tr></thead>`;
  const body = `<tbody>${mounted.map(a => `<tr>
    <td class="col-name">${a.name}</td>
    <td class="col-mount">${a.mount}</td>
  </tr>`).join('')}</tbody>`;
  return `<table class="arrays-table mounts-table">${head}${body}</table>`;
}

// ── Arrays table — one row per array, plus a toggle row when hidden entries exist ──

function makeArrayRow(arr) {
  const level = arr.level ? arr.level.replace(/^raid/i, 'RAID').toUpperCase() : '';
  const role  = (arr.role || 'data');
  const roleLabel = role.toUpperCase();

  let usageCell = '<td class="col-usage"></td>';
  if (hasValue(arr.used_pct)) {
    const cls = metricColor('disk_usage', arr.used_pct);
    usageCell = `<td class="col-usage ${cls}">${arr.used_pct.toFixed(1)}%</td>`;
  }

  const capacity = hasValue(arr.total_gb) ? formatCapacity(arr.total_gb) : '';
  const members  = (arr.members || []).join(' ');

  return `
    <tr>
      <td class="col-name">${arr.name}</td>
      <td class="col-level">${level}</td>
      <td class="col-capacity">${capacity}</td>
      ${usageCell}
      <td class="col-role role-${role}">${roleLabel}</td>
      <td class="col-members">${members}</td>
    </tr>`;
}

function makeArraysTable(arrays, machineName) {
  const expanded = ArraysExpanded.has(machineName);
  const visible = expanded ? arrays : arrays.filter(a => !a.hidden_by_default);
  const hiddenCount = arrays.length - visible.length;

  const head = `<thead><tr>
      <th class="col-name">Array</th>
      <th class="col-level">Level</th>
      <th class="col-capacity">Size</th>
      <th class="col-usage">Usage</th>
      <th class="col-role">Role</th>
      <th class="col-members">Members</th>
    </tr></thead>`;
  const rows = visible.map(makeArrayRow).join('');

  let toggleRow = '';
  if (hiddenCount > 0) {
    toggleRow = `<tr class="arrays-toggle-row"><td colspan="6">
      <button class="arrays-toggle" data-machine="${machineName}">
        <span class="toggle-icon">+</span> show ${hiddenCount} hidden ${hiddenCount === 1 ? 'array' : 'arrays'}
      </button>
    </td></tr>`;
  } else if (expanded && arrays.some(a => a.hidden_by_default)) {
    toggleRow = `<tr class="arrays-toggle-row"><td colspan="6">
      <button class="arrays-toggle" data-machine="${machineName}">
        <span class="toggle-icon">−</span> hide cache &amp; small arrays
      </button>
    </td></tr>`;
  }

  return `<table class="arrays-table">${head}<tbody>${rows}${toggleRow}</tbody></table>`;
}

// ── Disks tables — always two side-by-side <table> elements in a 2-col grid ──

function makeDiskRow(d) {
  const name     = d.name || '';
  const capacity = hasValue(d.total_gb) ? formatCapacity(d.total_gb) : '';

  let stateCell;
  if (hasValue(d.used_pct)) {
    const cls = metricColor('disk_usage', d.used_pct);
    stateCell = `<td class="col-state usage ${cls}">${d.used_pct.toFixed(1)}%</td>`;
  } else {
    const label = d.state && d.state !== 'mounted' ? d.state : 'unmounted';
    stateCell = `<td class="col-state label">${label}</td>`;
  }

  let tempCell = '<td class="col-temp"></td>';
  if (hasValue(d.temp)) {
    const cls = metricColor('disk_temp', d.temp);
    tempCell = `<td class="col-temp ${cls}">${d.temp.toFixed(1)}°C</td>`;
  }

  return `<tr>
    <td class="col-name">${name}</td>
    <td class="col-capacity">${capacity}</td>
    ${stateCell}${tempCell}
  </tr>`;
}

function makeDisksTable(disks) {
  const head = `<thead><tr>
    <th class="col-name">Disk</th>
    <th class="col-capacity">Size</th>
    <th class="col-state">State</th>
    <th class="col-temp">Temp</th>
  </tr></thead>`;
  const body = `<tbody>${disks.map(makeDiskRow).join('')}</tbody>`;
  return `<table class="disks-table">${head}${body}</table>`;
}

function makeCard(m) {
  const online     = m.online;
  const statusText = online ? 'online' : 'offline';
  const lastSeen   = online
    ? `${m.last_seen_seconds}s ago`
    : `last seen ${m.last_seen_seconds}s ago`;

  const hasGpu  = hasValue(m.gpu_usage);
  const disks   = Array.isArray(m.disks) ? m.disks : [];
  const arrays  = Array.isArray(m.arrays) ? m.arrays : [];
  const hasDisks  = disks.length > 0;
  const hasArrays = arrays.length > 0;

  const hwBadges = makeHwBadges(m);
  const osBadge  = makeOsBadge(m);

  // CPU + Memory + Motherboard row
  const cpuRow = `
    <div class="metric-group">
      <span class="group-title">System</span>
      <div class="metric-row">
        ${makeMetricCell('CPU Usage', m.cpu_usage, 'cpu_usage', '%', true)}
        ${makeMetricCell('CPU Temp', m.cpu_temp, 'cpu_temp', '°C', false)}
        ${makeMetricCell('Memory', m.memory_usage, 'memory_usage', '%', true)}
        ${makeMetricCell('Motherboard', m.motherboard_temp, 'motherboard_temp', '°C', false)}
      </div>
    </div>`;

  // GPU row (only if any GPU data present)
  const gpuRow = hasGpu ? `
    <hr class="divider">
    <div class="metric-group">
      <span class="group-title">GPU</span>
      <div class="metric-row">
        ${makeMetricCell('Usage', m.gpu_usage, 'gpu_usage', '%', true)}
        ${makeMetricCell('Memory', m.gpu_memory_usage, 'gpu_memory_usage', '%', true)}
        ${makeMetricCell('Temp', m.gpu_temp, 'gpu_temp', '°C', false)}
      </div>
    </div>` : '';

  // Arrays — full-width table; mount paths listed as "name: path" lines below it
  const arrayRow = hasArrays ? `
    <hr class="divider">
    <div class="metric-group">
      <span class="group-title">Arrays</span>
      ${makeArraysTable(arrays, m.machine_name)}
      ${makeMountsTable(arrays)}
    </div>` : '';

  // Disks — single full-width table so long state labels (e.g. "ntfs (not mounted)") fit
  const diskRow = hasDisks ? `
    <hr class="divider">
    <div class="metric-group">
      <span class="group-title">Disks</span>
      ${makeDisksTable(disks)}
    </div>` : '';

  return `
    <div class="card ${online ? '' : 'offline'}" data-machine="${m.machine_name}">
      <div class="card-header">
        <div class="card-header-row">
          <div class="machine-name">
            <span class="status-dot"></span>
            ${m.machine_name}
            ${makeTypeBadge(m)}
          </div>
          <div class="card-meta">
            <span>${lastSeen}</span>
            <span class="badge badge-${statusText}">${statusText}</span>
          </div>
        </div>
        ${osBadge ? `<div class="hw-badges hw-badges-os">${osBadge}</div>` : ''}
        ${hwBadges ? `<div class="hw-badges">${hwBadges}</div>` : ''}
      </div>
      <div class="card-body">
        ${cpuRow}
        ${gpuRow}
        ${arrayRow}
        ${diskRow}
      </div>
    </div>`;
}

// ── Update DOM ────────────────────────────────────────────────────────────────

function updateGrid(machines) {
  const grid = document.getElementById('machines-grid');

  if (!machines.length) {
    grid.innerHTML = '<div class="loading">No machines reporting yet.</div>';
    return;
  }

  // Rebuild all cards (simple approach — grid is small)
  grid.innerHTML = machines.map(makeCard).join('');
}

function updateTimestamp() {
  const el = document.getElementById('last-updated');
  const now = new Date();
  el.textContent = `Updated ${now.toLocaleTimeString()}`;
}

// ── Polling ───────────────────────────────────────────────────────────────────

async function poll() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateGrid(data);
    updateTimestamp();
  } catch (err) {
    console.error('Poll failed:', err);
  }
}

// Toggle hidden arrays per machine. Clicking re-polls so the next render uses
// the new expanded state immediately, instead of waiting for the next 5s tick.
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.arrays-toggle');
  if (!btn) return;
  const machine = btn.dataset.machine;
  if (!machine) return;
  if (ArraysExpanded.has(machine)) ArraysExpanded.delete(machine);
  else ArraysExpanded.add(machine);
  poll();
});

poll();
setInterval(poll, POLL_INTERVAL_MS);
