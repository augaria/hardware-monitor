import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from alerter import Alerter

app = Flask(__name__)

# { machine_name: { 'data': {...}, 'last_seen': float } }
_machines: dict = {}
_lock = threading.Lock()

OFFLINE_TIMEOUT = int(os.getenv('OFFLINE_TIMEOUT', '30'))

# Sort order on /api/status: server-class machines first, then NAS, then unknown.
_TYPE_ORDER = {'server': 0, 'nas': 1}

alerter = Alerter()


def _resolve_hide_threshold_gb(payload_thresholds):
    """Resolve the GB threshold below which arrays auto-collapse on the dashboard.
    Per-agent override > server env var (HIDE_ARRAYS_BELOW_GB) > 10."""
    if isinstance(payload_thresholds, dict):
        v = payload_thresholds.get('hide_arrays_below_gb')
        if isinstance(v, (int, float)):
            return float(v)
    env = os.getenv('HIDE_ARRAYS_BELOW_GB', '').strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return 10.0


def _decorate_arrays(data):
    """Mark arrays the dashboard should collapse by default (small or cache)."""
    arrays = data.get('arrays') or []
    if not arrays:
        return
    threshold_gb = _resolve_hide_threshold_gb(data.get('thresholds'))
    decorated = []
    for arr in arrays:
        a = dict(arr)
        small = a.get('total_gb') is not None and a['total_gb'] < threshold_gb
        cache = a.get('role') == 'cache'
        a['hidden_by_default'] = bool(small or cache)
        decorated.append(a)
    data['arrays'] = decorated


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    now = time.time()
    result = []
    with _lock:
        for name, entry in _machines.items():
            data = dict(entry['data'])  # shallow copy so decoration doesn't leak
            _decorate_arrays(data)
            age = now - entry['last_seen']
            result.append({
                **data,
                'online': age <= OFFLINE_TIMEOUT,
                'last_seen_seconds': int(age),
            })
    # Group by machine_type (server first, then nas), case-insensitive
    # alphabetical within each group
    result.sort(key=lambda m: (_TYPE_ORDER.get(m.get('machine_type'), 2),
                                m['machine_name'].lower()))
    return jsonify(result)


@app.route('/report', methods=['POST'])
def report():
    data = request.get_json(silent=True)
    if not data or 'machine_name' not in data:
        return jsonify({'error': 'invalid payload'}), 400

    name = data['machine_name']
    now = time.time()

    with _lock:
        prev = _machines.get(name)
        was_offline = prev is not None and (now - prev['last_seen']) > OFFLINE_TIMEOUT
        is_new = prev is None
        _machines[name] = {'data': data, 'last_seen': now}

    if not is_new:
        alerter.check(data, came_back_online=was_offline)

    return jsonify({'ok': True})


# ── Background: offline detection ─────────────────────────────────────────────

def _offline_watcher():
    notified: set = set()
    while True:
        time.sleep(10)
        now = time.time()
        with _lock:
            snapshot = list(_machines.items())

        for name, entry in snapshot:
            age = now - entry['last_seen']
            if age > OFFLINE_TIMEOUT:
                if name not in notified:
                    alerter.alert_offline(name, int(age))
                    notified.add(name)
            else:
                notified.discard(name)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    t = threading.Thread(target=_offline_watcher, daemon=True)
    t.start()

    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
