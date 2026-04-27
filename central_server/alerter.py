"""Alert logic: check incoming metric reports against configured thresholds
and dispatch notifications via the notifier library."""

import json
import os
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


# ── Alerter class ─────────────────────────────────────────────────────────────

class Alerter:
    def __init__(self):
        self._defaults = _load_default_thresholds()
        self._dispatcher = _build_dispatcher()
        cooldown_min = float(os.getenv('ALERT_COOLDOWN_MINUTES', '10'))
        self._cooldown_secs = cooldown_min * 60
        # key → last alert timestamp
        self._last_alert: dict[str, float] = {}

        if self._dispatcher:
            print(f"Alerter ready. Default thresholds: {self._defaults}  "
                  f"Cooldown: {cooldown_min}min")
        else:
            print("Alerter: no notification channels configured.")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _throttled(self, key: str) -> bool:
        """Return True if the alert is within cooldown window (suppress it)."""
        now = time.time()
        if now - self._last_alert.get(key, 0) < self._cooldown_secs:
            return True
        self._last_alert[key] = now
        return False

    def _send(self, level, title: str, body: str):
        if self._dispatcher is None:
            return
        try:
            self._dispatcher.notify(Notification(title=title, body=body, level=level))
        except Exception as exc:
            print(f"Alert dispatch failed: {exc}")

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, data: dict, came_back_online: bool = False):
        """Check a freshly received metric report against all configured thresholds."""
        machine = data.get('machine_name', 'unknown')

        if came_back_online:
            # Clear offline cooldown so the machine can go offline again later
            self._last_alert.pop(f"{machine}:offline", None)
            self._send(
                Level.INFO,
                f"[HW Monitor] {machine} back online",
                f"{machine} has resumed reporting to the central server.",
            )

        thresholds = _resolve_thresholds(self._defaults, data.get('thresholds'))

        for metric, threshold in thresholds.items():
            if metric == 'disk_temps':
                disks = data.get('disks')
                if not isinstance(disks, list):
                    continue
                for idx, disk in enumerate(disks):
                    v = disk.get('temp') if isinstance(disk, dict) else None
                    if isinstance(v, (int, float)) and v > threshold:
                        name = disk.get('name') or f"Disk {idx + 1}"
                        key = f"{machine}:disk_temps:{name}"
                        if not self._throttled(key):
                            self._send(
                                Level.WARNING,
                                f"[HW Monitor] {machine} High Disk Temperature",
                                f"{machine}: {name} temperature {v}°C exceeds threshold {threshold}°C",
                            )
                continue

            value = data.get(metric)
            if value is None:
                continue
            _, label, unit = _SCALAR_METRICS[metric]
            if isinstance(value, (int, float)) and value > threshold:
                key = f"{machine}:{metric}"
                if not self._throttled(key):
                    self._send(
                        Level.WARNING,
                        f"[HW Monitor] {machine} High {label}",
                        f"{machine}: {label} is {value}{unit}, threshold is {threshold}{unit}",
                    )

    def alert_offline(self, machine: str, age_seconds: int):
        """Called by the background watcher when a machine stops reporting."""
        key = f"{machine}:offline"
        if not self._throttled(key):
            self._send(
                Level.CRITICAL,
                f"[HW Monitor] {machine} offline",
                f"{machine} has not reported for {age_seconds}s "
                f"(timeout: {os.getenv('OFFLINE_TIMEOUT', '30')}s)",
            )
