/**
 * SMART ATTENDANCE ENTERPRISE - CLIENT APPLICATION LOGIC
 * Built with vanilla JavaScript & Modern Web Standards
 */

const API_BASE = '';
let currentEmployees = [];
let empPage = 1;
const EMP_PAGE_SIZE = 10;

// ==============================================================================
// 1. UTILITY FUNCTIONS & TOAST SYSTEM
// ==============================================================================

function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  
  const icon = type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ';
  toast.innerHTML = `
    <span style="font-weight: 700; font-size: 1rem;">${icon}</span>
    <div class="toast-msg">${message}</div>
  `;
  
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

function parseApiError(err) {
  if (err && err.detail) {
    if (Array.isArray(err.detail)) {
      return err.detail.map(e => `${e.loc?.slice(1).join('.') || 'field'}: ${e.msg}`).join('<br>');
    }
    return err.detail;
  }
  return err.message || 'An unexpected error occurred';
}

function formatISTTime(epochMs) {
  if (!epochMs) return '-';
  const d = new Date(epochMs);
  return d.toLocaleTimeString('en-US', { timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatISTDateTime(epochMs) {
  if (!epochMs) return '-';
  const d = new Date(epochMs);
  return d.toLocaleString('en-US', { timeZone: 'Asia/Kolkata', hour12: false });
}

function getTodayISTString() {
  const now = new Date();
  const istOffset = 5.5 * 60 * 60 * 1000;
  const istDate = new Date(now.getTime() + (now.getTimezoneOffset() * 60000) + istOffset);
  return istDate.toISOString().split('T')[0];
}

function getCurrentMonthString() {
  return getTodayISTString().substring(0, 7);
}

// Convert local datetime-local input string to epoch milliseconds
function localInputToEpochMs(inputValue) {
  if (!inputValue) return null;
  // Parse assuming IST (UTC+05:30)
  const dt = new Date(inputValue + ":00+05:30");
  return Math.floor(dt.getTime() / 1000) * 1000;
}

// ==============================================================================
// 2. LIVE CLOCK & HEALTH PING
// ==============================================================================

function startISTClock() {
  const clockEl = document.getElementById('istClock');
  function update() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false });
    if (clockEl) clockEl.textContent = timeStr;
  }
  update();
  setInterval(update, 1000);
}

async function checkDatabaseHealth() {
  const badge = document.getElementById('dbStatusBadge');
  const text = document.getElementById('dbStatusText');
  const t0 = performance.now();
  try {
    const res = await fetch(`${API_BASE}/health`);
    const ms = Math.round(performance.now() - t0);
    if (res.ok) {
      badge.classList.remove('error');
      text.textContent = `MongoDB Connected (${ms}ms)`;
    } else {
      badge.classList.add('error');
      text.textContent = 'DB Unavailable';
    }
  } catch (err) {
    badge.classList.add('error');
    text.textContent = 'Server Offline';
  }
}

// ==============================================================================
// 3. TAB NAVIGATION
// ==============================================================================

function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      
      btn.classList.add('active');
      const targetId = btn.getAttribute('data-tab');
      const targetEl = document.getElementById(targetId);
      if (targetEl) targetEl.classList.add('active');

      // Trigger refreshes for active tab
      if (targetId === 'employees') loadEmployees();
      if (targetId === 'attendance') loadAttendance();
      if (targetId === 'analytics') loadAllAnalytics();
    });
  });
}

// ==============================================================================
// 4. MODALS & POPUPS
// ==============================================================================

function initModals() {
  document.querySelectorAll('[data-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      const modalId = btn.getAttribute('data-close');
      const modal = document.getElementById(modalId);
      if (modal) modal.classList.remove('open');
    });
  });

  const openAddEmpBtn = document.getElementById('openAddEmpModalBtn');
  if (openAddEmpBtn) {
    openAddEmpBtn.addEventListener('click', () => {
      document.getElementById('addEmpModal').classList.add('open');
      document.getElementById('newEmpJoined').value = getTodayISTString();
    });
  }
}

// ==============================================================================
// 5. SEED SAMPLE DATA (1-CLICK TEST ENVIRONMENT)
// ==============================================================================

async function seedSampleData() {
  const btn = document.getElementById('seedDemoBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Seeding...';

  const samples = [
    {
      emp_code: "EMP1001",
      name: "Alice Smith",
      email: "alice@example.com",
      department: "Engineering",
      shift_start: "09:30",
      shift_end: "18:30",
      joined_on: "2026-01-15"
    },
    {
      emp_code: "EMP1002",
      name: "Bob Jones",
      email: "bob@example.com",
      department: "Engineering",
      shift_start: "09:30",
      shift_end: "18:30",
      joined_on: "2026-02-01"
    },
    {
      emp_code: "EMP1003",
      name: "Charlie Roy",
      email: "charlie@example.com",
      department: "Support",
      shift_start: "22:00",
      shift_end: "06:00",
      joined_on: "2026-01-10"
    }
  ];

  let added = 0;
  for (const emp of samples) {
    try {
      const res = await fetch(`${API_BASE}/employees`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(emp)
      });
      if (res.status === 201) added++;
    } catch (_) {}
  }

  showToast(`Sample data verified! Added ${added} new employees.`, 'success');
  btn.disabled = false;
  btn.textContent = '⚡ Seed Sample Data';

  await loadEmployees();
  await loadAttendance();
  await loadAllAnalytics();
}

// ==============================================================================
// 6. EMPLOYEE DIRECTORY & DROPDOWNS
// ==============================================================================

async function loadEmployees() {
  const dept = document.getElementById('empDeptFilter').value;
  const search = document.getElementById('empSearchInput').value.trim().toLowerCase();
  
  let url = `${API_BASE}/employees?page=${empPage}&page_size=${EMP_PAGE_SIZE}`;
  if (dept) url += `&department=${encodeURIComponent(dept)}`;

  try {
    const res = await fetch(url);
    const data = await res.json();
    currentEmployees = data.items || [];
    
    // Populate table
    const tbody = document.getElementById('employeesTableBody');
    tbody.innerHTML = '';

    let filtered = currentEmployees;
    if (search) {
      filtered = currentEmployees.filter(e => 
        e.emp_code.toLowerCase().includes(search) || 
        e.name.toLowerCase().includes(search) || 
        e.email.toLowerCase().includes(search)
      );
    }

    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-dim);">No employees found.</td></tr>`;
    } else {
      filtered.forEach(emp => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><strong>${emp.emp_code}</strong></td>
          <td>${emp.name}</td>
          <td>${emp.email}</td>
          <td><span class="chip chip-wfh">${emp.department}</span></td>
          <td>${emp.shift_start} - ${emp.shift_end} IST</td>
          <td>${emp.joined_on}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    // Pagination info
    const info = document.getElementById('empPaginationInfo');
    info.textContent = `Showing ${filtered.length} of ${data.total} employees (Page ${data.page})`;
    document.getElementById('empPrevPage').disabled = data.page <= 1;
    document.getElementById('empNextPage').disabled = (data.page * EMP_PAGE_SIZE) >= data.total;

    // Refresh dropdowns
    updateEmployeeDropdowns();
  } catch (err) {
    showToast(`Error loading employees: ${err.message}`, 'error');
  }
}

function updateEmployeeDropdowns() {
  const punchSelect = document.getElementById('punchEmpCode');
  const analyticsSelect = document.getElementById('analyticsEmpSelect');
  const deptFilter = document.getElementById('empDeptFilter');
  const attDeptFilter = document.getElementById('attDeptFilter');
  const trendDeptSelect = document.getElementById('trendDeptSelect');

  const selectedPunch = punchSelect.value;
  const selectedAnalytics = analyticsSelect.value;

  // Gather unique departments
  const depts = new Set();

  punchSelect.innerHTML = '<option value="">-- Choose Employee --</option>';
  analyticsSelect.innerHTML = '<option value="">-- Choose Employee --</option>';

  currentEmployees.forEach(emp => {
    depts.add(emp.department);
    const opt = `<option value="${emp.emp_code}">${emp.emp_code} - ${emp.name} (${emp.department})</option>`;
    punchSelect.insertAdjacentHTML('beforeend', opt);
    analyticsSelect.insertAdjacentHTML('beforeend', opt);
  });

  if (selectedPunch) punchSelect.value = selectedPunch;
  if (selectedAnalytics) analyticsSelect.value = selectedAnalytics;
  else if (currentEmployees.length > 0) {
    analyticsSelect.value = currentEmployees[0].emp_code;
  }

  // Update department dropdowns
  depts.forEach(d => {
    if (!Array.from(deptFilter.options).some(o => o.value === d)) {
      deptFilter.insertAdjacentHTML('beforeend', `<option value="${d}">${d}</option>`);
      attDeptFilter.insertAdjacentHTML('beforeend', `<option value="${d}">${d}</option>`);
      trendDeptSelect.insertAdjacentHTML('beforeend', `<option value="${d}">${d}</option>`);
    }
  });

  if (depts.size > 0 && !trendDeptSelect.value) {
    trendDeptSelect.value = Array.from(depts)[0];
  }
}

async function handleAddEmployee(e) {
  e.preventDefault();
  const payload = {
    emp_code: document.getElementById('newEmpCode').value.trim(),
    name: document.getElementById('newEmpName').value.trim(),
    email: document.getElementById('newEmpEmail').value.trim(),
    department: document.getElementById('newEmpDept').value.trim(),
    shift_start: document.getElementById('newEmpShiftStart').value.trim(),
    shift_end: document.getElementById('newEmpShiftEnd').value.trim(),
    joined_on: document.getElementById('newEmpJoined').value.trim(),
  };

  try {
    const res = await fetch(`${API_BASE}/employees`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw data;

    showToast(`Employee ${data.emp_code} successfully created!`, 'success');
    document.getElementById('addEmpModal').classList.remove('open');
    document.getElementById('addEmpForm').reset();
    await loadEmployees();
  } catch (err) {
    showToast(parseApiError(err), 'error');
  }
}

// ==============================================================================
// 7. PUNCH DESK ACTIONS
// ==============================================================================

function initPunchDesk() {
  const punchEmpSelect = document.getElementById('punchEmpCode');
  const shiftHint = document.getElementById('selectedEmpShiftHint');
  const modeRadios = document.querySelectorAll('input[name="punchMode"]');
  const customGroup = document.getElementById('customTimestampGroup');

  punchEmpSelect.addEventListener('change', () => {
    const emp = currentEmployees.find(e => e.emp_code === punchEmpSelect.value);
    if (emp) {
      shiftHint.innerHTML = `Shift: <strong>${emp.shift_start} to ${emp.shift_end} IST</strong> | Grace: 10 mins`;
    } else {
      shiftHint.textContent = '';
    }
  });

  modeRadios.forEach(radio => {
    radio.addEventListener('change', () => {
      customGroup.style.display = radio.value === 'custom' ? 'block' : 'none';
      if (radio.value === 'custom' && !document.getElementById('customPunchDateTime').value) {
        const now = new Date();
        const localIso = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
        document.getElementById('customPunchDateTime').value = localIso;
      }
    });
  });

  document.getElementById('btnPunchIn').addEventListener('click', () => executePunch('in'));
  document.getElementById('btnPunchOut').addEventListener('click', () => executePunch('out'));
}

async function executePunch(action) {
  const empCode = document.getElementById('punchEmpCode').value;
  if (!empCode) {
    showToast('Please select an employee first', 'error');
    return;
  }

  const isCustom = document.querySelector('input[name="punchMode"]:checked').value === 'custom';
  let punchedAt = null;
  if (isCustom) {
    const inputVal = document.getElementById('customPunchDateTime').value;
    if (!inputVal) {
      showToast('Please enter a custom timestamp', 'error');
      return;
    }
    punchedAt = localInputToEpochMs(inputVal);
  }

  const endpoint = action === 'in' ? '/attendance/punch-in' : '/attendance/punch-out';
  const payload = { emp_code: empCode };
  if (punchedAt) payload.punched_at = punchedAt;
  if (action === 'in') {
    payload.status = document.getElementById('punchStatus').value;
  }

  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw data;

    showToast(`Punch ${action.toUpperCase()} successful for ${empCode}!`, 'success');
    renderPunchReceipt(data);
    await loadAttendance();
  } catch (err) {
    showToast(parseApiError(err), 'error');
  }
}

function renderPunchReceipt(record) {
  const area = document.getElementById('punchReceiptArea');
  const lateBadge = record.late_minutes > 0 
    ? `<span class="chip chip-late">${record.late_minutes} min late</span>` 
    : `<span class="chip chip-present">On Time</span>`;
  
  const otBadge = record.overtime_minutes > 0
    ? `<span class="chip chip-wfh">+${record.overtime_minutes} min OT</span>`
    : `<span style="color: var(--text-dim); font-size: 0.75rem;">None</span>`;

  const halfDayBadge = record.half_day ? `<span class="chip chip-halfday">Half Day (&lt; 4.5h)</span>` : '';

  area.innerHTML = `
    <div style="background: rgba(15, 23, 42, 0.45); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); padding: 1.25rem;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.85rem;">
        <div>
          <span style="font-size: 1.15rem; font-weight: 700; color: #fff;">${record.emp_code}</span>
          <span style="margin-left: 0.5rem; font-size: 0.82rem; color: var(--text-muted);">${record.date}</span>
        </div>
        <span class="chip chip-${record.status.toLowerCase()}">${record.status}</span>
      </div>

      <div class="grid-2" style="gap: 0.75rem; margin-bottom: 1rem;">
        <div>
          <div style="font-size: 0.7rem; color: var(--text-dim); text-transform: uppercase;">Punch In</div>
          <div style="font-size: 0.95rem; font-weight: 600; color: #fff;">${formatISTTime(record.punch_in)}</div>
        </div>
        <div>
          <div style="font-size: 0.7rem; color: var(--text-dim); text-transform: uppercase;">Punch Out</div>
          <div style="font-size: 0.95rem; font-weight: 600; color: #fff;">${formatISTTime(record.punch_out)}</div>
        </div>
      </div>

      <div style="border-top: 1px solid var(--border-subtle); padding-top: 0.75rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem;">
        <div>
          <div style="font-size: 0.7rem; color: var(--text-dim);">WORK HOURS</div>
          <div style="font-size: 1.1rem; font-weight: 700; color: var(--primary);">${record.work_hours !== null ? record.work_hours + ' hrs' : 'In Progress'}</div>
        </div>
        <div style="display: flex; gap: 0.4rem; align-items: center;">
          ${lateBadge}
          ${otBadge}
          ${halfDayBadge}
        </div>
      </div>
    </div>
  `;
}

// ==============================================================================
// 8. ATTENDANCE LOGS & REGULARIZATION
// ==============================================================================

async function loadAttendance() {
  const date = document.getElementById('attDateFilter').value;
  const dept = document.getElementById('attDeptFilter').value;
  const status = document.getElementById('attStatusFilter').value;

  let url = `${API_BASE}/attendance?page=1&page_size=50`;
  if (date) url += `&date=${encodeURIComponent(date)}`;
  if (dept) url += `&department=${encodeURIComponent(dept)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;

  try {
    const res = await fetch(url);
    const data = await res.json();
    const tbody = document.getElementById('attendanceTableBody');
    tbody.innerHTML = '';

    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-dim);">No attendance records found for criteria.</td></tr>`;
      return;
    }

    data.items.forEach(log => {
      const statusClass = log.status.toLowerCase();
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${log.emp_code}</strong></td>
        <td>${log.date}</td>
        <td><span class="chip chip-${statusClass}">${log.status}</span></td>
        <td>${formatISTTime(log.punch_in)}</td>
        <td>${formatISTTime(log.punch_out)}</td>
        <td>${log.work_hours !== null ? log.work_hours : '-'}</td>
        <td>${log.late_minutes > 0 ? `<span style="color: var(--danger);">${log.late_minutes}</span>` : '0'}</td>
        <td>${log.overtime_minutes > 0 ? `<span style="color: var(--success);">+${log.overtime_minutes}</span>` : '0'}</td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="openRegularizeModal('${log.emp_code}', '${log.date}')">
            ✏️ Regularize
          </button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    showToast(`Failed to load attendance logs: ${err.message}`, 'error');
  }
}

window.openRegularizeModal = async function(empCode, dateStr) {
  document.getElementById('regEmpCode').value = empCode;
  document.getElementById('regDate').value = dateStr;
  document.getElementById('regHeaderCode').textContent = empCode;
  document.getElementById('regHeaderDate').textContent = dateStr;
  document.getElementById('regReason').value = '';
  document.getElementById('regManager').value = 'MGR001';

  // Load record details to show audit history
  try {
    const res = await fetch(`${API_BASE}/attendance?emp_code=${empCode}&date=${dateStr}`);
    const data = await res.json();
    const item = data.items && data.items[0];

    const histSection = document.getElementById('regHistorySection');
    const timeline = document.getElementById('regHistoryTimeline');
    timeline.innerHTML = '';

    if (item && item.history && item.history.length > 0) {
      histSection.style.display = 'block';
      item.history.forEach(h => {
        const changesText = Object.entries(h.changes).map(([k, v]) => `${k}: ${v.from} &rarr; ${v.to}`).join(', ');
        timeline.insertAdjacentHTML('beforeend', `
          <div class="history-item">
            <div class="history-meta">${formatISTDateTime(h.at)} by <strong>${h.by}</strong></div>
            <div class="history-reason">"${h.reason}"</div>
            <div style="font-size: 0.72rem; color: var(--text-dim); margin-top: 0.15rem;">${changesText}</div>
          </div>
        `);
      });
    } else {
      histSection.style.display = 'none';
    }
  } catch (_) {}

  document.getElementById('regularizeModal').classList.add('open');
};

async function handleRegularizeSubmit(e) {
  e.preventDefault();
  const empCode = document.getElementById('regEmpCode').value;
  const dateStr = document.getElementById('regDate').value;
  const status = document.getElementById('regStatus').value;
  const punchInVal = document.getElementById('regPunchIn').value;
  const punchOutVal = document.getElementById('regPunchOut').value;
  const reason = document.getElementById('regReason').value.trim();
  const regBy = document.getElementById('regManager').value.trim();

  const payload = {
    reason: reason,
    regularized_by: regBy
  };
  if (status) payload.status = status;
  if (punchInVal) payload.punch_in = localInputToEpochMs(punchInVal);
  if (punchOutVal) payload.punch_out = localInputToEpochMs(punchOutVal);

  try {
    const res = await fetch(`${API_BASE}/attendance/${empCode}/${dateStr}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw data;

    showToast(`Record ${empCode} for ${dateStr} regularized!`, 'success');
    document.getElementById('regularizeModal').classList.remove('open');
    await loadAttendance();
  } catch (err) {
    showToast(parseApiError(err), 'error');
  }
}

// ==============================================================================
// 9. ANALYTICS HUB
// ==============================================================================

async function loadAllAnalytics() {
  const month = document.getElementById('analyticsMonthPicker').value || getCurrentMonthString();
  const empCode = document.getElementById('analyticsEmpSelect').value;
  const trendDept = document.getElementById('trendDeptSelect').value;

  if (empCode) await loadMonthlyEmpAnalytics(empCode, month);
  await loadLateLeaderboard(month);
  await loadDeptSummary(month);
  if (trendDept) await loadDeptTrend(trendDept, month);
}

async function loadMonthlyEmpAnalytics(empCode, month) {
  try {
    const res = await fetch(`${API_BASE}/analytics/employees/${empCode}/monthly?month=${month}`);
    const data = await res.json();
    if (!res.ok) throw data;

    document.getElementById('monthlyEmpBadge').textContent = `${empCode} (${month})`;
    document.getElementById('statWorkingDays').textContent = data.working_days;
    document.getElementById('statPresentDays').textContent = data.present_days;
    document.getElementById('statAttendancePct').textContent = data.attendance_pct !== null ? `${data.attendance_pct}%` : 'N/A';
    document.getElementById('statLateCount').textContent = data.late_count;
    document.getElementById('statTotalLateMins').textContent = `${data.total_late_minutes} total late mins (${data.total_overtime_minutes}m OT)`;
  } catch (err) {
    document.getElementById('monthlyEmpBadge').textContent = 'Error loading employee stats';
  }
}

async function loadLateLeaderboard(month) {
  try {
    const res = await fetch(`${API_BASE}/analytics/leaderboard/late?month=${month}&limit=10`);
    const data = await res.json();
    const tbody = document.getElementById('leaderboardTableBody');
    tbody.innerHTML = '';

    if (!data.leaderboard || data.leaderboard.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-dim);">No late attendance recorded for ${month}.</td></tr>`;
      return;
    }

    data.leaderboard.forEach(item => {
      const rankClass = item.rank === 1 ? 'rank-1' : item.rank === 2 ? 'rank-2' : item.rank === 3 ? 'rank-3' : 'rank-other';
      const medal = item.rank === 1 ? '🥇' : item.rank === 2 ? '🥈' : item.rank === 3 ? '🥉' : item.rank;
      
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="rank-badge ${rankClass}">${medal}</span></td>
        <td><strong>${item.emp_code}</strong></td>
        <td><span style="color: var(--danger); font-weight: 600;">${item.late_count}</span></td>
        <td>${item.total_late_minutes} mins</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    showToast(`Leaderboard error: ${err.message}`, 'error');
  }
}

async function loadDeptSummary(month) {
  try {
    const res = await fetch(`${API_BASE}/analytics/departments/summary?month=${month}`);
    const data = await res.json();
    const tbody = document.getElementById('deptSummaryTableBody');
    tbody.innerHTML = '';

    if (!data.departments || data.departments.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-dim);">No department records for ${month}.</td></tr>`;
      return;
    }

    data.departments.forEach(d => {
      const rateText = d.attendance_pct !== null ? `${d.attendance_pct}%` : 'N/A';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${d.department}</strong></td>
        <td>${d.headcount}</td>
        <td><strong style="color: var(--primary);">${rateText}</strong></td>
        <td>${d.late_count}</td>
        <td>+${d.total_overtime_minutes}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    showToast(`Dept summary error: ${err.message}`, 'error');
  }
}

async function loadDeptTrend(department, month) {
  try {
    const [y, m] = month.split('-').map(Number);
    const lastDay = new Date(y, m, 0).getDate();
    const from = `${month}-01`;
    const to = `${month}-${String(lastDay).padStart(2, '0')}`;
    const res = await fetch(`${API_BASE}/analytics/departments/${encodeURIComponent(department)}/trend?from=${from}&to=${to}`);
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to load trend');
    }
    const data = await res.json();
    const points = Array.isArray(data) ? data : (data.trend || []);
    renderTrendSvg(points);
  } catch (err) {
    showToast(`Trend error: ${err.message}`, 'error');
  }
}

function renderTrendSvg(points) {
  const svg = document.getElementById('trendSvg');
  svg.innerHTML = '';

  if (points.length === 0) {
    svg.innerHTML = `<text x="250" y="120" text-anchor="middle" fill="#64748b" font-size="12">No data points for this month</text>`;
    return;
  }

  const width = 500;
  const height = 240;
  const padLeft = 40;
  const padRight = 20;
  const padTop = 20;
  const padBottom = 35;

  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  // Grid lines (0%, 25%, 50%, 75%, 100%)
  for (let pct = 0; pct <= 100; pct += 25) {
    const y = padTop + chartH - (pct / 100 * chartH);
    svg.insertAdjacentHTML('beforeend', `
      <line x1="${padLeft}" y1="${y}" x2="${width - padRight}" y2="${y}" class="chart-grid-line" />
      <text x="${padLeft - 6}" y="${y + 4}" text-anchor="end" fill="#64748b" font-size="10">${pct}%</text>
    `);
  }

  // Calculate coordinates
  const n = points.length;
  const coords = points.map((p, i) => {
    const x = padLeft + (i / Math.max(1, n - 1)) * chartW;
    const rate = p.attendance_rate !== null ? p.attendance_rate : 0;
    const y = padTop + chartH - (rate * chartH);
    return { x, y, point: p };
  });

  // Area & Line Paths
  const lineD = coords.reduce((acc, c, i) => `${acc} ${i === 0 ? 'M' : 'L'} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`, '');
  const areaD = `${lineD} L ${coords[coords.length - 1].x.toFixed(1)} ${padTop + chartH} L ${coords[0].x.toFixed(1)} ${padTop + chartH} Z`;

  // Defs gradient
  svg.insertAdjacentHTML('beforeend', `
    <defs>
      <linearGradient id="trendGradient" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#6366f1" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="#6366f1" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    <path d="${areaD}" class="chart-area" />
    <path d="${lineD}" class="chart-line" />
  `);

  // Dots with tooltips
  coords.forEach((c, idx) => {
    const dateStr = c.point.date.slice(-2);
    // Draw day label every few days
    if (idx % 4 === 0 || idx === n - 1) {
      svg.insertAdjacentHTML('beforeend', `
        <text x="${c.x}" y="${height - 10}" text-anchor="middle" fill="#94a3b8" font-size="10">${dateStr}</text>
      `);
    }

    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', c.x);
    dot.setAttribute('cy', c.y);
    dot.setAttribute('r', '4');
    dot.setAttribute('class', 'chart-dot');
    
    const rateText = c.point.attendance_rate !== null ? `${(c.point.attendance_rate * 100).toFixed(0)}%` : 'Weekend/Holiday';
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `Date: ${c.point.date}\nAttendance: ${rateText}\nMoving Avg: ${c.point.moving_avg_7d ? (c.point.moving_avg_7d * 100).toFixed(1) + '%' : '-'}`;
    dot.appendChild(title);
    svg.appendChild(dot);
  });
}

// ==============================================================================
// 10. QUERY EXPLAIN INSPECTOR
// ==============================================================================

async function runQueryExplain() {
  const ep = document.getElementById('explainEndpointSelect').value;
  const btn = document.getElementById('btnRunExplain');
  btn.disabled = true;
  btn.textContent = 'Inspecting...';

  try {
    const res = await fetch(`${API_BASE}/admin/explain/${ep}`);
    const data = await res.json();

    document.getElementById('explainTimeMs').textContent = `${data.execution_time_ms} ms`;
    document.getElementById('explainStage').textContent = data.winning_plan_stage || 'N/A';
    document.getElementById('explainDocs').textContent = data.total_docs_examined !== null ? data.total_docs_examined : 'N/A';
    document.getElementById('explainKeys').textContent = data.total_keys_examined !== null ? data.total_keys_examined : 'N/A';

    document.getElementById('explainJsonViewer').textContent = JSON.stringify(data.raw_explain, null, 2);
    showToast(`Explain plan executed for ${ep}!`, 'success');
  } catch (err) {
    showToast(`Explain failed: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Inspect Execution';
  }
}

// ==============================================================================
// 11. INITIALIZATION ON DOM READY
// ==============================================================================

document.addEventListener('DOMContentLoaded', async () => {
  startISTClock();
  initTabs();
  initModals();
  initPunchDesk();

  // Set default dates
  const todayIST = getTodayISTString();
  const currentMonth = getCurrentMonthString();
  document.getElementById('attDateFilter').value = todayIST;
  document.getElementById('analyticsMonthPicker').value = currentMonth;

  // Event Listeners
  document.getElementById('seedDemoBtn').addEventListener('click', seedSampleData);
  document.getElementById('addEmpForm').addEventListener('submit', handleAddEmployee);
  document.getElementById('regularizeForm').addEventListener('submit', handleRegularizeSubmit);
  document.getElementById('btnRefreshAttendance').addEventListener('click', loadAttendance);
  document.getElementById('btnRefreshAnalytics').addEventListener('click', loadAllAnalytics);
  document.getElementById('btnRunExplain').addEventListener('click', runQueryExplain);

  document.getElementById('empSearchInput').addEventListener('input', () => {
    loadEmployees();
  });
  document.getElementById('empDeptFilter').addEventListener('change', () => {
    empPage = 1;
    loadEmployees();
  });
  document.getElementById('trendDeptSelect').addEventListener('change', (e) => {
    const month = document.getElementById('analyticsMonthPicker').value || getCurrentMonthString();
    loadDeptTrend(e.target.value, month);
  });
  document.getElementById('analyticsEmpSelect').addEventListener('change', (e) => {
    const month = document.getElementById('analyticsMonthPicker').value || getCurrentMonthString();
    if (e.target.value) loadMonthlyEmpAnalytics(e.target.value, month);
  });

  // Health check & Initial loads
  await checkDatabaseHealth();
  setInterval(checkDatabaseHealth, 30000);

  await loadEmployees();
  await loadAttendance();
  await loadAllAnalytics();
});
