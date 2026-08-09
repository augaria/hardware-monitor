"""Alert logic: check incoming metric reports against configured thresholds
and dispatch notifications via the notifier library.

A single sample over the line is not an alert. Temperatures and memory are
point samples, so one compile job or one backup used to be enough to trigger a
notification. Every threshold is therefore debounced: the value must stay over
the line continuously for ALERT_FOR_MINUTES before anything is sent.

Clearing is hysteretic — the value has to fall to ALERT_CLEAR_RATIO × threshold
(and stay there just as long) before the alert resolves. Without that gap, a
metric sitting right on the line would alternate between firing and resolving.

Debounce and repeat interval are separate concerns: ALERT_FOR_MINUTES decides
*whether* something is real, ALERT_REPEAT_HOURS decides how often you are
reminded about it while it stays broken.
"""

import json
import math
import os
import threading
import time

try:
    from notifier import NotificationDispatcher, Notification, Level
    from notifier.channels.email import EmailConfig, EmailNotifier
    from notifier.channels.wechat import WeChatConfig, WeChatNotifier
    from notifier.core.dispatcher import register as _register

    # Ensure channel types are registered (some versions require explicit import)
    try:
        _register(EmailConfig, EmailNotifier)
    except Exception:
        pass
    try:
        _register(WeChatConfig, WeChatNotifier)
    except Exception:
        pass

    NOTIFIER_AVAILABLE = True
except ImportError:
    NOTIFIER_AVAILABLE = False
    print("Warning: notifier package not installed — alerts disabled.")


# ── Threshold definitions ─────────────────────────────────────────────────────

# Maps metric key → (env var name, display label, unit)
_SCALAR_METRICS = {
    'cpu_usage':        ('ALERT_CPU_USAGE',        'CPU Usage',             '%'),
    'cpu_temp':         ('ALERT_CPU_TEMP',          'CPU Temperature',       '°C'),
    'memory_usage':     ('ALERT_MEMORY_USAGE',      'Memory Usage',          '%'),
    'motherboard_temp': ('ALERT_MOTHERBOARD_TEMP',  'Motherboard Temp',      '°C'),
    'gpu_usage':        ('ALERT_GPU_USAGE',          'GPU Usage',             '%'),
    'gpu_temp':         ('ALERT_GPU_TEMP',           'GPU Temperature',       '°C'),
    'gpu_memory_usage': ('ALERT_GPU_MEMORY',         'GPU Memory Usage',      '%'),
}

_DISK_TEMP_ENV = 'ALERT_DISK_TEMP'


def _load_default_thresholds() -> dict:
    """Read server-wide default thresholds from environment variables."""
    thresholds = {}
    for metric, (env_key, _, _unit) in _SCALAR_METRICS.items():
        val = os.getenv(env_key, '').strip()
        if val:
            try:
                thresholds[metric] = float(val)
            except ValueError:
                print(f"Warning: invalid value for {env_key}={val!r}")

    val = os.getenv(_DISK_TEMP_ENV, '').strip()
    if val:
        try:
            thresholds['disk_temps'] = float(val)
        except ValueError:
            print(f"Warning: invalid value for {_DISK_TEMP_ENV}={val!r}")

    return thresholds


def _resolve_thresholds(defaults: dict, overrides) -> dict:
    """Merge per-agent overrides on top of server defaults, key-by-key.

    Each metric is independent: an agent that overrides only cpu_temp keeps
    using the server default for every other metric.
    """
    if not isinstance(overrides, dict) or not overrides:
        return defaults
    valid_metrics = set(_SCALAR_METRICS) | {'disk_temps'}
    merged = dict(defaults)
    for k, v in overrides.items():
        if k not in valid_metrics:
            continue
        if isinstance(v, (int, float)):
            merged[k] = float(v)
    return merged


def _build_dispatcher():
    if not NOTIFIER_AVAILABLE:
        return None

    raw = os.getenv('NOTIFIER_CHANNELS', '[]').strip()
    if not raw or raw == '[]':
        return None

    try:
        channels_conf = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error parsing NOTIFIER_CHANNELS JSON: {exc}")
        return None

    configs = []
    for ch in channels_conf:
        ch = dict(ch)
        ch_type = ch.pop('type', '').lower()
        try:
            if ch_type == 'email':
                configs.append(EmailConfig(**{k: v for k, v in ch.items()
                                              if k in ('smtp_server', 'email', 'passkey', 'recipients', 'min_level')}))
            elif ch_type == 'wechat':
                configs.append(WeChatConfig(**{k: v for k, v in ch.items()
                                               if k in ('app_id', 'app_secret', 'user_id', 'template_id',
                                                        'token_cache_path', 'min_level')}))
            else:
                print(f"Warning: unknown channel type {ch_type!r}")
        except Exception as exc:
            print(f"Error configuring {ch_type} channel: {exc}")

    if not configs:
        return None

    try:
        return NotificationDispatcher.from_configs(configs)
    except Exception as exc:
        print(f"Error building NotificationDispatcher: {exc}")
        return None


# ── Per-condition state ───────────────────────────────────────────────────────

class _State:
    """Tracking for one alertable condition (one metric on one machine)."""

    __slots__ = ('breach_since', 'samples', 'last_at', 'clear_since', 'alerted_at')

    def __init__(self, breach_since=None, samples=0, last_at=None,
                 clear_since=None, alerted_at=None):
        self.breach_since = breach_since   # when the current breach started
        self.samples = samples             # samples seen during this breach
        self.last_at = last_at             # when the last breaching sample arrived
        self.clear_since = clear_since     # when the value fell below the clear line
        self.alerted_at = alerted_at       # when we last notified about it

    @property
    def alerting(self) -> bool:
        return self.alerted_at is not None

    def to_dict(self) -> dict:
        return {'breach_since': self.breach_since, 'samples': self.samples,
                'last_at': self.last_at, 'clear_since': self.clear_since,
                'alerted_at': self.alerted_at}

    @classmethod
    def from_dict(cls, d: dict) -> '_State':
        return cls(d.get('breach_since'), int(d.get('samples', 0)),
                   d.get('last_at'), d.get('clear_since'), d.get('alerted_at'))


# ── Alerter class ─────────────────────────────────────────────────────────────

class Alerter:
    def __init__(self):
        self._defaults = _load_default_thresholds()
        self._dispatcher = _build_dispatcher()

        self._for_secs = float(os.getenv('ALERT_FOR_MINUTES', '5')) * 60
        self._repeat_secs = float(os.getenv('ALERT_REPEAT_HOURS', '6')) * 3600
        self._clear_ratio = float(os.getenv('ALERT_CLEAR_RATIO', '0.95'))
        # Longest gap between reports that still counts as a continuous breach.
        # OFFLINE_TIMEOUT is already sized at several report intervals.
        self._max_gap = float(os.getenv('OFFLINE_TIMEOUT', '30'))
        self._state_path = os.getenv('ALERT_STATE_PATH', '/data/alert_state.json')

        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._load_state()

        if self._dispatcher:
            print(f"Alerter ready. Default thresholds: {self._defaults}  "
                  f"Debounce: {self._for_secs / 60:g}min  "
                  f"Repeat: {self._repeat_secs / 3600:g}h  "
                  f"Clear at: {self._clear_ratio:g}x threshold")
        else:
            print("Alerter: no notification channels configured.")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _due(self, state: _State, now: float) -> bool:
        """True if this condition may notify now (never alerted, or repeat is due)."""
        return state.alerted_at is None or now - state.alerted_at >= self._repeat_secs

    def _send(self, level, title: str, body: str):
        print(f"Alert: {title} — {body}", flush=True)
        if self._dispatcher is None:
            return
        try:
            self._dispatcher.notify(Notification(title=title, body=body, level=level))
        except Exception as exc:
            print(f"Alert dispatch failed: {exc}")

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self):
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            self._states = {k: _State.from_dict(v) for k, v in data.items()}
            print(f"Alerter: restored state for {len(self._states)} conditions "
                  f"from {self._state_path}")
        except Exception as exc:
            print(f"Alerter: could not load state from {self._state_path}: {exc}")

    def _save_state(self):
        """Persist state so a restart does not re-alert everything still hot."""
        if not self._state_path:
            return
        tmp = self._state_path + '.tmp'
        try:
            os.makedirs(os.path.dirname(self._state_path) or '.', exist_ok=True)
            with open(tmp, 'w') as f:
                json.dump({k: v.to_dict() for k, v in self._states.items()}, f)
            os.replace(tmp, self._state_path)
        except Exception as exc:
            print(f"Alerter: could not persist state to {self._state_path}: {exc}")

    # ── Threshold evaluation ──────────────────────────────────────────────────

    def _evaluate(self, key: str, value: float, threshold: float,
                  title: str, unit: str, label: str, machine: str) -> None:
        """Feed one sample into the state machine for *key* and notify if due.

        Called with the lock held.
        """
        now = time.time()
        state = self._states.setdefault(key, _State())

        if value > threshold:
            state.clear_since = None
            # A gap in reporting breaks continuity: without samples in between
            # we have no evidence the value stayed high, so the breach restarts.
            # Two samples an hour apart must not satisfy "high for 5 minutes".
            gap = state.last_at is not None and now - state.last_at > self._max_gap
            if state.breach_since is None or gap:
                state.breach_since = now
                state.samples = 1
            else:
                state.samples += 1
            state.last_at = now

            # Both a sustained duration and more than one sample, so a machine
            # that reappears cannot alert on the first report it sends.
            sustained = (now - state.breach_since >= self._for_secs
                         and state.samples >= 2)
            if sustained and self._due(state, now):
                minutes = math.floor((now - state.breach_since) / 60)
                state.alerted_at = now
                self._send(
                    Level.WARNING,
                    f"[HW Monitor] {machine} High {title}",
                    f"{machine}: {label} is {value}{unit}, threshold is "
                    f"{threshold}{unit} (over the line for ~{minutes} min)",
                )
            return

        # Below the threshold — reset the breach, then decide about clearing.
        state.breach_since = None
        state.samples = 0
        state.last_at = now

        if not state.alerting:
            self._states.pop(key, None)
            return

        # Hysteresis: only treat it as resolved once the value drops clearly
        # below the line, otherwise a metric hovering at the threshold would
        # alternate between firing and resolving.
        if value > threshold * self._clear_ratio:
            state.clear_since = None
            return

        if state.clear_since is None:
            state.clear_since = now
        elif now - state.clear_since >= self._for_secs:
            self._states.pop(key, None)
            self._send(
                Level.WARNING,
                f"[HW Monitor] {machine} {title} back to normal",
                f"{machine}: {label} is {value}{unit}, "
                f"below the {threshold}{unit} threshold again",
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, data: dict):
        """Check a freshly received metric report against all configured thresholds."""
        machine = data.get('machine_name', 'unknown')
        thresholds = _resolve_thresholds(self._defaults, data.get('thresholds'))

        with self._lock:
            # Any report clears the offline condition. Doing this unconditionally
            # (rather than only on an observed offline→online edge) matters after
            # a central-server restart: the in-memory last_seen map is gone, so
            # the edge is invisible, and stale offline state would otherwise
            # suppress the next genuine offline alert for a whole repeat window.
            if self._states.pop(f"{machine}:offline", _State()).alerting:
                self._send(
                    # WARNING, not INFO: notifier channels are configured with
                    # min_level=WARNING and drop anything below it silently
                    # (the gate reports success), so an INFO notice never arrives.
                    Level.WARNING,
                    f"[HW Monitor] {machine} back online",
                    f"{machine} has resumed reporting to the central server.",
                )

            for metric, threshold in thresholds.items():
                if metric == 'disk_temps':
                    disks = data.get('disks')
                    if not isinstance(disks, list):
                        continue
                    for idx, disk in enumerate(disks):
                        v = disk.get('temp') if isinstance(disk, dict) else None
                        if not isinstance(v, (int, float)):
                            continue
                        name = disk.get('name') or f"Disk {idx + 1}"
                        self._evaluate(
                            f"{machine}:disk_temps:{name}", v, threshold,
                            title='Disk Temperature', unit='°C',
                            label=f"{name} temperature", machine=machine,
                        )
                    continue

                value = data.get(metric)
                if not isinstance(value, (int, float)):
                    continue
                _, label, unit = _SCALAR_METRICS[metric]
                self._evaluate(f"{machine}:{metric}", value, threshold,
                               title=label, unit=unit, label=label, machine=machine)

            self._save_state()

    def alert_offline(self, machine: str, age_seconds: int):
        """Called by the background watcher while a machine is not reporting.

        Safe to call on every watcher tick: the repeat interval decides whether
        anything is actually sent. OFFLINE_TIMEOUT is the debounce here — the
        machine has already been silent that long by the time we are called.
        """
        key = f"{machine}:offline"
        now = time.time()
        with self._lock:
            state = self._states.setdefault(key, _State())
            if not self._due(state, now):
                return
            state.alerted_at = now
            self._save_state()

        self._send(
            Level.CRITICAL,
            f"[HW Monitor] {machine} offline",
            f"{machine} has not reported for {age_seconds}s "
            f"(timeout: {os.getenv('OFFLINE_TIMEOUT', '30')}s)",
        )
