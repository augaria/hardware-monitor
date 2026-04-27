#!/usr/bin/env python3
"""Hardware Monitor Agent — collects system metrics and pushes to central server."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import psutil
except ImportError:
    print("Error: psutil is required. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)


# ── Static info (collected once, included in every report) ────────────────────

def detect_vm():
    """Return (is_vm, virt_type). Uses systemd-detect-virt; falls back to False."""
    try:
        result = subprocess.run(
            ['systemd-detect-virt'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=2,
        )
        virt = result.stdout.strip()
        if virt and virt != 'none':
            return True, virt
    except Exception:
        pass
    return False, None


def clean_cpu_model(raw):
    s = raw
    for pat in (r'\(R\)', r'\(TM\)', r'\(tm\)',
                r'\bAMD\b', r'\bIntel\b',
                r'\s*CPU\s*@\s*[\d.]+\s*\w+',
                r'\bCPU\b',
                r'\b\d+-Core Processor\b',
                r'\bProcessor\b'):
        s = re.sub(pat, '', s)
    return re.sub(r'\s+', ' ', s).strip()


def get_cpu_model():
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    return clean_cpu_model(line.split(':', 1)[1])
    except Exception:
        pass
    return None


def clean_gpu_model(raw):
    s = re.sub(r'NVIDIA\s+', '', raw).strip()
    return re.sub(r'\s+', ' ', s)


def _read_kv_file(path):
    """Parse a simple shell-style KEY=VALUE file. Returns {} on any error."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                out[k.strip()] = v.strip().strip('"').strip("'").strip()
    except Exception:
        pass
    return out


def detect_machine_type(os_name, hostname):
    """Return 'nas' or 'server' — drives sorting and the type badge in the UI.

    Strong NAS signals (any one is enough):
      - /etc/openmediavault/  exists  (OMV — runs on Debian, won't show in os_name)
      - /etc/truenas/ or /usr/local/etc/middleware/ exists  (TrueNAS Scale/Core)
      - os_name contains DSM / Synology / TrueNAS / QNAP / Unraid
      - hostname contains nas / synology / truenas / qnap / unraid
    """
    if os.path.isdir('/etc/openmediavault'):
        return 'nas'
    if os.path.isdir('/etc/truenas') or os.path.isdir('/usr/local/etc/middleware'):
        return 'nas'
    nas_kw = ('dsm', 'synology', 'truenas', 'freenas', 'qnap', 'unraid')
    if os_name and any(kw in os_name.lower() for kw in nas_kw):
        return 'nas'
    if hostname:
        h = hostname.lower()
        if 'nas' in h or any(kw in h for kw in ('synology', 'truenas', 'qnap', 'unraid')):
            return 'nas'
    return 'server'


def get_os_name():
    """Return human-readable OS name like 'Ubuntu 24.04.4 LTS'.

    Tries /etc/os-release first (covers Ubuntu, Debian, Arch, Alpine, Fedora,
    DSM 7+, OpenWrt, etc.); falls back to Synology's /etc.defaults/VERSION.
    """
    info = _read_kv_file('/etc/os-release')
    if info.get('PRETTY_NAME'):
        return info['PRETTY_NAME']
    if info.get('NAME'):
        ver = info.get('VERSION') or info.get('VERSION_ID')
        return f"{info['NAME']} {ver}".strip() if ver else info['NAME']

    syno = _read_kv_file('/etc.defaults/VERSION')
    os_name = syno.get('os_name')
    if os_name:
        ver = syno.get('productversion')
        build = syno.get('buildnumber')
        parts = [os_name]
        if ver:
            parts.append(ver)
        if build:
            parts.append(f"({build})")
        return ' '.join(parts)
    return None


# ── Per-agent threshold overrides ─────────────────────────────────────────────

# Recognized override keys → normalized name sent in payload thresholds dict.
# The ALERT_* keys feed the central server's alerter; HIDE_ARRAYS_BELOW_GB
# feeds a UI display threshold (small arrays auto-collapse on the dashboard).
_THRESHOLD_KEYS = {
    'ALERT_CPU_USAGE':        'cpu_usage',
    'ALERT_CPU_TEMP':         'cpu_temp',
    'ALERT_MEMORY_USAGE':     'memory_usage',
    'ALERT_MOTHERBOARD_TEMP': 'motherboard_temp',
    'ALERT_GPU_USAGE':        'gpu_usage',
    'ALERT_GPU_TEMP':         'gpu_temp',
    'ALERT_GPU_MEMORY':       'gpu_memory_usage',
    'ALERT_DISK_TEMP':        'disk_temps',
    'HIDE_ARRAYS_BELOW_GB':   'hide_arrays_below_gb',
}


def load_thresholds(path):
    """Load per-agent threshold overrides from a KEY=VALUE file.

    Returns a dict keyed by central-server metric name, e.g.
    {'cpu_usage': 85.0, 'disk_temps': 55.0}. Missing/empty values are skipped
    so that the central server's defaults remain in effect for those metrics.
    Returns {} if the file is missing or unreadable.
    """
    if not path or not os.path.isfile(path):
        return {}

    overrides = {}
    raw = _read_kv_file(path)
    for env_key, value in raw.items():
        metric = _THRESHOLD_KEYS.get(env_key)
        if metric is None:
            print(f"[thresholds] unknown key {env_key!r} in {path} — ignored", flush=True)
            continue
        if not value:
            continue
        try:
            overrides[metric] = float(value)
        except ValueError:
            print(f"[thresholds] invalid number {value!r} for {env_key} in {path} — ignored", flush=True)
    return overrides


# ── Metric collectors ──────────────────────────────────────────────────────────

def get_cpu_usage():
    return round(psutil.cpu_percent(interval=None), 1)


def get_memory():
    vm = psutil.virtual_memory()
    return round(vm.percent, 1), round(vm.total / (1024 ** 3), 1), round(vm.used / (1024 ** 3), 1)


def get_cpu_temp(temps):
    for key in ('k10temp', 'coretemp'):
        if key in temps:
            return round(temps[key][0].current, 1)
    return None


def get_motherboard_temp(temps):
    """Try common motherboard/chipset sensor sources."""
    if 'acpitz' in temps:
        return round(temps['acpitz'][0].current, 1)
    for name, sensors in temps.items():
        if any(name.startswith(prefix) for prefix in ('it8', 'nct', 'w83', 'asus', 'adt', 'f71', 'lm')):
            for s in sensors:
                label = s.label.lower()
                if any(kw in label for kw in ('system', 'systin', 'mb', 'board')):
                    return round(s.current, 1)
            # Fallback: first sensor of the chip
            return round(sensors[0].current, 1)
    return None


def get_gpu_info():
    """Return (model, usage_pct, memory_pct, temp) or (None,)*4."""
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()
        model = clean_gpu_model(name)
        gpu_usage = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        gpu_memory_usage = mem.used / mem.total * 100
        gpu_temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        pynvml.nvmlShutdown()
        return model, round(float(gpu_usage), 1), round(gpu_memory_usage, 1), round(float(gpu_temp), 1)
    except Exception:
        return None, None, None, None


def _nvme_temp(dev, temps):
    """Match /sys/block/nvmeNn1 to its Composite temperature.

    Kernels vary: some expose one sensor key per drive (nvme0, nvme1, ...),
    others group all drives under a single 'nvme' key with multiple Composite
    entries in drive-index order.
    """
    # Per-drive key: /sys/block/nvme0n1 → 'nvme0'
    sensor_key = re.sub(r'n\d+$', '', dev)
    sensors = temps.get(sensor_key)
    if sensors:
        for s in sensors:
            if s.label == 'Composite':
                return round(s.current, 1)

    # Shared 'nvme' key: pick the N-th Composite by drive index
    m = re.match(r'nvme(\d+)n\d+$', dev)
    if m and 'nvme' in temps:
        idx = int(m.group(1))
        composites = [s for s in temps['nvme'] if s.label == 'Composite']
        if idx < len(composites):
            return round(composites[idx].current, 1)

    return None


def _nvme_sysfs_temp(dev):
    """Read NVMe temperature from /sys/block/<dev>/device/hwmon*/temp1_input.

    /sys/block/nvme0n1/device points at the NVMe controller node, which on
    most modern kernels exposes hwmonN/temp1_input (Composite, millidegrees).
    Bypasses psutil — useful on kernels that don't surface NVMe via the
    standard sensors API (notably Synology DSM).
    """
    base = f'/sys/block/{dev}/device'
    try:
        entries = os.listdir(base)
    except Exception:
        return None
    for entry in entries:
        if not entry.startswith('hwmon'):
            continue
        path = f'{base}/{entry}/temp1_input'
        try:
            with open(path) as f:
                return round(int(f.read().strip()) / 1000, 1)
        except Exception:
            continue
    return None


def _synonvme_temp(dev):
    """Synology DSM-only NVMe temperature read via /usr/syno/bin/synonvme.

    DSM 6/7 doesn't expose NVMe via hwmon and ships smartmontools 6.5
    (2016) which fails NVMe Admin commands, so Synology's own utility is
    the only working path on DSM hardware. Silent no-op when the binary
    doesn't exist (every non-DSM system).

    Tries two subcommands in order — if Synology ever deprecates the
    direct one in a future DSM, --smart-info-get gives the same
    "Temperature: NN C" line as part of a broader SMART dump and is
    less likely to be removed.
    """
    binpath = '/usr/syno/bin/synonvme'
    if not os.path.isfile(binpath):
        return None
    for flag in ('--temperature-get', '--smart-info-get'):
        try:
            result = subprocess.run(
                ['sudo', binpath, flag, f'/dev/{dev}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=5,
            )
            m = re.search(r'Temperature:\s*(-?\d+)\s*C', result.stdout)
            if m:
                return float(m.group(1))
        except Exception:
            continue
    return None


def _smartctl_nvme_temp(dev):
    """Parse the Temperature line from `smartctl -A` NVMe output.

    NVMe SMART log uses a key-value layout, not the numbered ATA attribute
    table that _smartctl_temp parses. Last-resort fallback when neither
    psutil nor sysfs hwmon expose the sensor.
    """
    try:
        result = subprocess.run(
            ['sudo', 'smartctl', '-A', f'/dev/{dev}'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=5,
        )
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('Temperature:'):
                m = re.search(r'(-?\d+)\s*Celsius', line)
                if m:
                    return float(m.group(1))
    except Exception:
        pass
    return None


def _smartctl_temp(dev):
    try:
        result = subprocess.run(
            ['sudo', 'smartctl', '-A', f'/dev/{dev}'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=5,
        )
        for line in result.stdout.split('\n'):
            parts = line.split()
            if len(parts) < 10:
                continue
            if parts[0] in ('194', '190') or 'Temperature' in parts[1]:
                try:
                    return float(parts[9])
                except (ValueError, IndexError):
                    return None
    except Exception:
        pass
    return None


def _device_sysfs_name(path):
    """Resolve /dev/<X> to its /sys/block/<name> entry via major:minor.

    Needed because /dev/mapper/* on DSM are real block-device files (not
    symlinks), so realpath alone returns "cachedev_0" instead of "dm-2".
    Going through major:minor + /sys/dev/block/<maj>:<min> works on every
    modern Linux/DSM kernel.
    """
    try:
        st = os.stat(path)
        major = os.major(st.st_rdev)
        minor = os.minor(st.st_rdev)
        link = os.readlink(f'/sys/dev/block/{major}:{minor}')
        return link.split('/')[-1]
    except Exception:
        return None


def _build_mount_index():
    """Map block-device sysfs name → (total_bytes, used_pct, mountpoint)."""
    out = {}
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            name = _device_sysfs_name(part.device)
            if not name:
                # Fallback: tmpfs / overlay / etc. with no /sys/block entry
                try:
                    real = os.path.realpath(part.device)
                except Exception:
                    real = part.device
                name = real.split('/')[-1]
            prev = out.get(name)
            if prev is None or u.total > prev[0]:
                out[name] = (u.total, round(u.percent, 1), part.mountpoint)
    except Exception:
        pass
    return out


def _read_swap_devices():
    """Return set of sysfs device names actively used as swap (from /proc/swaps)."""
    out = set()
    try:
        with open('/proc/swaps') as f:
            next(f, None)  # header
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                name = _device_sysfs_name(parts[0]) or parts[0].split('/')[-1]
                out.add(name)
    except Exception:
        pass
    return out


def _read_mdstat():
    """Parse /proc/mdstat. Returns list of arrays with name, level, state, members."""
    arrays = []
    try:
        with open('/proc/mdstat') as f:
            for line in f:
                m = re.match(r'^(md\d+)\s*:\s*(\w+)\s+(\w+)\s+(.+)$', line)
                if not m:
                    continue
                name, state, level, rest = m.groups()
                members = []
                for tok in rest.split():
                    # Tokens look like 'sda3[0]', 'nvme0n1p3[1]', 'sdb[2](S)' (spare)
                    mm = re.match(r'([a-zA-Z][a-zA-Z0-9_]*?)(?:p?\d+)?\[\d+\]', tok)
                    if mm:
                        members.append(mm.group(1))
                arrays.append({
                    'name': name,
                    'level': level,
                    'state': state,
                    'members': sorted(set(members)),
                })
    except Exception:
        pass
    return arrays


def _holders_of(node):
    """List names in /sys/block/<disk>/holders/ — works for both whole-disks and partitions."""
    # Whole-disk path
    p = f'/sys/block/{node}/holders'
    if os.path.isdir(p):
        try:
            return os.listdir(p)
        except Exception:
            return []
    # Partition path: scan /sys/block/*/<node>/holders
    try:
        for blk in os.listdir('/sys/block'):
            pp = f'/sys/block/{blk}/{node}/holders'
            if os.path.isdir(pp):
                try:
                    return os.listdir(pp)
                except Exception:
                    return []
    except Exception:
        pass
    return []


def _all_holders_of_disk(dev):
    """Union of holders for a disk and all its partitions (one level deep)."""
    holders = set(_holders_of(dev))
    base = f'/sys/block/{dev}'
    try:
        for entry in os.listdir(base):
            if entry.startswith(dev) and entry != dev:
                holders.update(_holders_of(entry))
    except Exception:
        pass
    return holders


def _walk_to_top(node, mount_index, _depth=0):
    """Recursively follow holders to find the topmost device that has a mount entry.

    Returns (total_bytes, used_pct, mountpoint) or None if nothing on top is mounted.
    """
    if _depth > 8:
        return None
    if node in mount_index:
        return mount_index[node]
    for h in _holders_of(node):
        info = _walk_to_top(h, mount_index, _depth + 1)
        if info:
            return info
    return None


def _disk_partition_usage(dev, mount_index):
    """If any partition of <dev> is mounted (directly or via holders), pick the largest."""
    best = None
    try:
        entries = os.listdir(f'/sys/block/{dev}')
    except Exception:
        return None
    for entry in entries:
        if not entry.startswith(dev) or entry == dev:
            continue
        info = mount_index.get(entry) or _walk_to_top(entry, mount_index)
        if info is None:
            continue
        if best is None or info[0] > best[0]:
            best = info
    return best


def _blkid_type(dev):
    try:
        result = subprocess.run(
            ['blkid', '-o', 'value', '-s', 'TYPE', f'/dev/{dev}'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=2,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _disk_partitions_of(dev):
    try:
        entries = os.listdir(f'/sys/block/{dev}')
    except Exception:
        return []
    return [e for e in entries if e.startswith(dev) and e != dev]


def _sysfs_size_bytes(dev):
    try:
        with open(f'/sys/block/{dev}/size') as f:
            return int(f.read()) * 512
    except Exception:
        return 0


def _slaves_contain(dev, target, _depth=0):
    """True if target appears anywhere in dev's /sys/block/<dev>/slaves chain (walks down)."""
    if _depth > 8:
        return False
    p = f'/sys/block/{dev}/slaves'
    if not os.path.isdir(p):
        return False
    try:
        slaves = os.listdir(p)
    except Exception:
        return False
    if target in slaves:
        return True
    for s in slaves:
        if _slaves_contain(s, target, _depth + 1):
            return True
    return False


def _find_mount_over_array(arr_name, mount_index):
    """Find a mount whose underlying slaves chain contains arr_name.

    Needed on Synology DSM where the SSD cache layer sits between md devices and
    the volume mount and doesn't expose the link via /sys/.../holders/.
    """
    for mounted_dev, info in mount_index.items():
        if _slaves_contain(mounted_dev, arr_name):
            return info
    return None


def _classify_disk(dev, mdstat_by_size, mount_index):
    """Return (used_pct_or_None, state_string).

    Priority:
      1. Member of an mdadm array  → `RAID mdN` (NO direct mount attribution,
         even if a partition of this disk feeds a root-fs array — attributing the
         root's used% to a 14 TB NAS drive is worse than useless)
      2. Built under LVM / bcache  → `LVM PV` / `SSD cache` / `LUKS (locked)`
      3. Whole-disk or partition mounted directly → `mounted` + usage
      4. Filesystem-signature classification via blkid
    """
    # 1. mdadm membership wins. When a disk belongs to multiple arrays (DSM's
    # tiny md0+md1 system partitions plus the big md2 data array), surface the
    # largest so the chip says "RAID md2" not "RAID md0".
    for arr in mdstat_by_size:
        if dev in arr['members']:
            return None, f"RAID {arr['name']}"

    # 2. Holders chain (whole disk + partition holders) reveals LVM/bcache/etc.
    for h in _all_holders_of_disk(dev):
        if h.startswith('md'):
            return None, f"RAID {h}"
        if h.startswith('bcache'):
            return None, "SSD cache"
        if h.startswith('dm-'):
            fstype = _blkid_type(dev)
            if fstype == 'crypto_LUKS':
                return None, "LUKS (locked)"
            # LVM PV: typical case is one disk → one VG → one or more mounted LVs.
            # Walk the partition chain through dm-* to find the largest mounted
            # LV and attribute its usage to the disk. For multi-PV setups this
            # over-attributes (each PV reports the same LV usage), but it gives
            # the operator the info they care about; pure ambiguity ("LVM PV"
            # with no number) was the worse default.
            info = (mount_index.get(dev)
                    or _walk_to_top(dev, mount_index)
                    or _disk_partition_usage(dev, mount_index))
            if info is not None:
                return info[1], 'mounted'
            return None, "LVM PV"

    # 3. Direct mount (whole disk or largest mounted partition)
    info = mount_index.get(dev) or _disk_partition_usage(dev, mount_index)
    if info is not None:
        return info[1], 'mounted'

    # 4. Filesystem signature fallback
    fstype = _blkid_type(dev)
    if fstype == 'zfs_member':
        return None, "ZFS pool"
    if fstype == 'crypto_LUKS':
        return None, "LUKS (locked)"
    if fstype == 'linux_raid_member':
        return None, "RAID member (inactive)"
    if fstype:
        return None, f"{fstype} (not mounted)"
    for part in _disk_partitions_of(dev):
        pt_fs = _blkid_type(part)
        if pt_fs and pt_fs != 'linux_raid_member':
            return None, f"{pt_fs} (not mounted)"
    return None, "unmounted"


def get_arrays(mdstat, mount_index):
    """For each mdadm array, find the mount on top (if any) and report capacity + usage.

    Three-phase lookup:
      1. Walk /sys/block/<md>/holders/ upward (standard Linux layout).
      2. Scan every mount's slaves chain downward (catches mounts where the
         /sys link goes the other way).
      3. Fall back to capacity matching against unattributed dm-* mounts. This
         bridges Synology DSM's SSD-cache module which mounts /volumeN on a
         cachedev that links via /sys to md3 (cache) but not to md2 (data) —
         the link from cache to backing array is hidden inside DSM's kernel
         module. Matching by filesystem size recovers the association without
         hardcoding any DSM-specific names.

    When the discovered mount's filesystem is much larger than the array's raw
    size (>1.25x), treat the array as a subordinate (cache) layer — show raw
    size only, not the misleading filesystem used%.
    """
    swap_devs = _read_swap_devices()
    out = []
    owned_mounts = set()

    for arr in mdstat:
        raw_bytes = _sysfs_size_bytes(arr['name'])
        info = _walk_to_top(arr['name'], mount_index) or _find_mount_over_array(arr['name'], mount_index)

        role = 'data'
        total_gb = used_pct = mount = None
        if info:
            mount_total, mount_used_pct, mount_point = info
            if raw_bytes and mount_total > raw_bytes * 1.25:
                # Subordinate layer (e.g. SSD cache feeding a much larger volume)
                total_gb = round(raw_bytes / (1024 ** 3), 1)
                role = 'cache'
            else:
                total_gb = round(mount_total / (1024 ** 3), 1)
                used_pct = mount_used_pct
                mount = mount_point
                owned_mounts.add(mount_point)
        elif raw_bytes:
            total_gb = round(raw_bytes / (1024 ** 3), 1)
            # All-NVMe array with no mount: most likely a write/read cache
            if arr['members'] and all(m.startswith('nvme') for m in arr['members']):
                role = 'cache'

        if arr['name'] in swap_devs:
            role = 'swap'

        out.append({
            'name': arr['name'],
            'level': arr['level'],
            'state': arr['state'],
            'role': role,
            'members': arr['members'],
            'total_gb': total_gb,
            'used_pct': used_pct,
            'mount': mount,
        })

    # Phase 3: capacity matching for arrays still without a mount.
    # Restrict candidates to dm-* mounts (volume sits behind LVM / cache /
    # crypt) so we don't accidentally match a raw single-disk filesystem.
    free_dm_mounts = [
        (dev, info) for dev, info in mount_index.items()
        if dev.startswith('dm-') and info[2] not in owned_mounts
    ]
    for arr in out:
        if arr['mount'] is not None or not arr['total_gb']:
            continue
        arr_bytes = arr['total_gb'] * 1024 ** 3
        candidates = [
            (dev, info) for dev, info in free_dm_mounts
            if abs(info[0] - arr_bytes) / arr_bytes < 0.10
        ]
        if len(candidates) != 1:
            continue
        dev, (mount_total, mount_used_pct, mount_point) = candidates[0]
        arr['total_gb'] = round(mount_total / (1024 ** 3), 1)
        arr['used_pct'] = mount_used_pct
        arr['mount'] = mount_point
        # A successful capacity match means this array IS the data-bearing one
        # (the cache layer was probably matched in phase 1 and rejected by the
        # subordinate-size rule, leaving the volume free for us)
        if arr.get('role') != 'swap':
            arr['role'] = 'data'
        owned_mounts.add(mount_point)
        free_dm_mounts = [(d, i) for d, i in free_dm_mounts if i[2] != mount_point]

    return out


def get_disks(temps):
    """Return list of {name, total_gb, used_pct, temp, state} for each physical disk."""
    mount_index = _build_mount_index()
    mdstat = _read_mdstat()
    # Larger arrays take priority when a disk belongs to multiple (DSM's tiny
    # system arrays md0/md1/md2 all share sata1..sataN; we want the chip to say
    # "RAID md2" for the data array, not "RAID md0" for the 2 GB root).
    mdstat_by_size = sorted(mdstat, key=lambda a: _sysfs_size_bytes(a['name']), reverse=True)

    try:
        block_devs = sorted(os.listdir('/sys/block'))
    except Exception:
        return [], []

    disks = []
    for dev in block_devs:
        # Skip pseudo-devices, software RAID/DM nodes (those go in `arrays`),
        # and Synology's bootloader flash (synoboot, synoboot1..N).
        if dev.startswith(('loop', 'ram', 'dm-', 'sr', 'zram', 'md', 'synoboot')):
            continue
        try:
            with open(f'/sys/block/{dev}/size') as f:
                total_bytes = int(f.read()) * 512
        except Exception:
            continue
        if total_bytes == 0:
            continue

        used_pct, state = _classify_disk(dev, mdstat_by_size, mount_index)

        if dev.startswith('nvme'):
            # Per-controller sysfs hwmon is the most reliable source —
            # psutil's index-based fallback can mis-order temperatures when
            # all NVMe sensors are bundled under a single 'nvme' key. On
            # DSM none of the standard paths work (no hwmon, smartctl 6.5
            # can't speak NVMe), so synonvme is the only thing that does.
            temp = (_nvme_sysfs_temp(dev)
                    or _nvme_temp(dev, temps)
                    or _synonvme_temp(dev)
                    or _smartctl_nvme_temp(dev))
        elif dev.startswith(('sd', 'hd', 'sata')):
            temp = _smartctl_temp(dev)
        else:
            temp = None

        disks.append({
            'name': dev,
            'total_gb': round(total_bytes / (1024 ** 3), 1),
            'used_pct': used_pct,
            'temp': temp,
            'state': state,
        })
    return disks, get_arrays(mdstat, mount_index)


# ── Main collection + reporting ───────────────────────────────────────────────

def collect(machine_name, static):
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        temps = {}

    gpu_model, gpu_usage, gpu_memory_usage, gpu_temp = get_gpu_info()
    mem_pct, mem_total_gb, mem_used_gb = get_memory()
    disks, arrays = get_disks(temps)

    payload = {
        'machine_name': machine_name,
        # Static hardware info
        'is_vm': static['is_vm'],
        'virt_type': static['virt_type'],
        'os_name': get_os_name(),
        'machine_type': static['machine_type'],
        'cpu_model': static['cpu_model'],
        'cpu_cores': static['cpu_cores'],
        'memory_total_gb': mem_total_gb,
        'gpu_model': gpu_model,
        # Live metrics
        'cpu_usage': get_cpu_usage(),
        'cpu_temp': get_cpu_temp(temps),
        'memory_usage': mem_pct,
        'memory_used_gb': mem_used_gb,
        'motherboard_temp': get_motherboard_temp(temps),
        'gpu_usage': gpu_usage,
        'gpu_memory_usage': gpu_memory_usage,
        'gpu_temp': gpu_temp,
        'disks': disks,
        'arrays': arrays,
    }
    if static.get('thresholds'):
        payload['thresholds'] = static['thresholds']
    return payload


def report(data, url):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main():
    parser = argparse.ArgumentParser(description='Hardware Monitor Agent')
    parser.add_argument('--server', required=True,
                        help='Central server URL, e.g. http://192.168.1.100:5000')
    parser.add_argument('--name', default=socket.gethostname(),
                        help='Machine name reported to server (default: hostname)')
    parser.add_argument('--interval', type=int, default=60,
                        help='Report interval in seconds (default: 60)')
    parser.add_argument('--thresholds', default=None,
                        help='Optional path to a per-agent threshold override file '
                             '(see thresholds.sample.conf). Read once at startup; '
                             'restart the service after edits.')
    args = parser.parse_args()

    report_url = f"{args.server.rstrip('/')}/report"

    is_vm, virt_type = detect_vm()
    os_name = get_os_name()
    thresholds = load_thresholds(args.thresholds) if args.thresholds else {}
    static = {
        'is_vm': is_vm,
        'virt_type': virt_type,
        'machine_type': detect_machine_type(os_name, args.name),
        'cpu_model': get_cpu_model(),
        'cpu_cores': psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True),
        'thresholds': thresholds,
    }

    print(f"Hardware Monitor Agent starting — "
          f"machine={args.name}  server={report_url}  interval={args.interval}s  "
          f"vm={is_vm}  os={os_name!r}  cpu={static['cpu_model']!r}  "
          f"thresholds={thresholds or '(none — using server defaults)'}",
          flush=True)

    while True:
        try:
            data = collect(args.name, static)
            status = report(data, report_url)
            print(f"[OK {status}] {data}", flush=True)
        except Exception as e:
            print(f"[ERR] {e}", flush=True)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
