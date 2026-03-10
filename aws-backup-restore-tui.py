#!/usr/bin/env python3
"""
EC2 AWS Backup Restore TUI
===========================
A terminal user interface for AWS CloudShell to restore EC2 instances
from AWS Backup recovery points.

Two restore modes:
  1. Replace existing instance (terminate current, restore with same IP/ENI)
  2. Restore to new instance and attach an existing ENI

Requirements: boto3, curses (both available in AWS CloudShell)
Usage:        python3 aws-backup-restore-tui.py

Keyboard shortcuts:
  ↑/↓ or j/k  Navigate lists
  PgUp/PgDn   Page through lists
  g / Home    Jump to top
  G / End     Jump to bottom
  /           Search/filter current list
  Enter       Select item
  q / Esc     Go back / cancel
  ?           Show help
"""

import curses
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("ERROR: boto3 is required. Run in AWS CloudShell or install boto3.")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# AWS Client Helpers
# ──────────────────────────────────────────────────────────────────────

class AWSClients:
    """Lazy-initialised AWS service clients."""

    def __init__(self):
        self._session = boto3.session.Session()
        self._region = self._session.region_name or "ap-southeast-2"
        self._ec2 = None
        self._backup = None
        self._sts = None
        self._account_id: Optional[str] = None

    @property
    def region(self) -> str:
        return self._region

    @property
    def ec2(self):
        if self._ec2 is None:
            self._ec2 = self._session.client("ec2", region_name=self._region)
        return self._ec2

    @property
    def backup(self):
        if self._backup is None:
            self._backup = self._session.client("backup", region_name=self._region)
        return self._backup

    @property
    def sts(self):
        if self._sts is None:
            self._sts = self._session.client("sts", region_name=self._region)
        return self._sts

    def get_account_id(self) -> str:
        if self._account_id is None:
            self._account_id = self.sts.get_caller_identity()["Account"]
        return self._account_id


aws = AWSClients()

# Session-scoped restore job history
_restore_jobs: List[Dict] = []


# ──────────────────────────────────────────────────────────────────────
# Data-fetching functions
# ──────────────────────────────────────────────────────────────────────

def list_backup_vaults() -> List[Dict]:
    """Return backup vaults with metadata, sorted by name."""
    vaults = []
    paginator = aws.backup.get_paginator("list_backup_vaults")
    for page in paginator.paginate():
        for v in page.get("BackupVaultList", []):
            vaults.append({
                "name": v["BackupVaultName"],
                "arn": v.get("BackupVaultArn", ""),
                "recovery_points": v.get("NumberOfRecoveryPoints", 0),
                "encryption": v.get("EncryptionKeyArn", ""),
            })
    return sorted(vaults, key=lambda x: x["name"])


def list_recovery_points(vault_name: str) -> List[Dict]:
    """Return EC2 recovery points from a vault, newest first."""
    points = []
    paginator = aws.backup.get_paginator("list_recovery_points_by_backup_vault")
    for page in paginator.paginate(BackupVaultName=vault_name):
        for rp in page.get("RecoveryPoints", []):
            arn = rp.get("ResourceArn", "")
            rtype = rp.get("ResourceType", "")
            if rtype == "EC2" or ":instance/" in arn:
                points.append(rp)
    points.sort(
        key=lambda r: r.get("CreationDate", datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return points


def list_ec2_instances() -> List[Dict]:
    """Return running/stopped EC2 instances."""
    instances = []
    paginator = aws.ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                )
                instances.append({
                    "InstanceId": inst["InstanceId"],
                    "Name": name,
                    "State": inst["State"]["Name"],
                    "InstanceType": inst.get("InstanceType", ""),
                    "PrivateIp": inst.get("PrivateIpAddress", "N/A"),
                    "PublicIp": inst.get("PublicIpAddress", "N/A"),
                    "SubnetId": inst.get("SubnetId", ""),
                    "VpcId": inst.get("VpcId", ""),
                    "AvailabilityZone": inst.get("Placement", {}).get("AvailabilityZone", ""),
                    "NetworkInterfaces": inst.get("NetworkInterfaces", []),
                    "SecurityGroups": [sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
                    "IamInstanceProfile": inst.get("IamInstanceProfile", {}).get("Arn", ""),
                    "KeyName": inst.get("KeyName", ""),
                    "LaunchTime": inst.get("LaunchTime", ""),
                })
    return instances


def list_enis() -> List[Dict]:
    """Return available (not in-use) ENIs."""
    enis = []
    paginator = aws.ec2.get_paginator("describe_network_interfaces")
    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        for eni in page["NetworkInterfaces"]:
            name = next(
                (t["Value"] for t in eni.get("TagSet", []) if t["Key"] == "Name"), ""
            )
            enis.append({
                "NetworkInterfaceId": eni["NetworkInterfaceId"],
                "Name": name,
                "SubnetId": eni.get("SubnetId", ""),
                "VpcId": eni.get("VpcId", ""),
                "AvailabilityZone": eni.get("AvailabilityZone", ""),
                "PrivateIp": eni.get("PrivateIpAddress", "N/A"),
                "Description": eni.get("Description", ""),
                "SecurityGroups": [sg["GroupId"] for sg in eni.get("Groups", [])],
            })
    return enis


def vault_name_from_rp_arn(rp_arn: str) -> str:
    """Extract backup vault name from a recovery point ARN."""
    # ARN: arn:aws:backup:region:acct:backup-vault:VaultName/recovery-point-id
    if ":backup-vault:" in rp_arn:
        return rp_arn.split(":backup-vault:")[-1].split("/")[0]
    return rp_arn.split(":")[-1].split("/")[0]


def fetch_restore_metadata(rp_arn: str) -> Dict:
    """Retrieve restore metadata for a recovery point."""
    resp = aws.backup.get_recovery_point_restore_metadata(
        BackupVaultName=vault_name_from_rp_arn(rp_arn),
        RecoveryPointArn=rp_arn,
    )
    return resp.get("RestoreMetadata", {})


def get_default_restore_role_arn() -> str:
    return f"arn:aws:iam::{aws.get_account_id()}:role/service-role/AWSBackupDefaultServiceRole"


def start_restore_job(rp_arn: str, metadata: Dict, iam_role_arn: str) -> str:
    resp = aws.backup.start_restore_job(
        RecoveryPointArn=rp_arn,
        Metadata=metadata,
        IamRoleArn=iam_role_arn,
        ResourceType="EC2",
    )
    return resp["RestoreJobId"]


def get_restore_job_status(job_id: str) -> Dict:
    return aws.backup.describe_restore_job(RestoreJobId=job_id)


def terminate_instance(instance_id: str):
    aws.ec2.terminate_instances(InstanceIds=[instance_id])


def wait_for_instance_terminated(instance_id: str):
    waiter = aws.ec2.get_waiter("instance_terminated")
    waiter.wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 10, "MaxAttempts": 60},
    )


def wait_for_eni_available(eni_id: str, timeout: int = 300):
    waiter = aws.ec2.get_waiter("network_interface_available")
    waiter.wait(
        NetworkInterfaceIds=[eni_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": timeout // 5},
    )


def attach_eni(eni_id: str, instance_id: str, device_index: int = 1):
    aws.ec2.attach_network_interface(
        NetworkInterfaceId=eni_id,
        InstanceId=instance_id,
        DeviceIndex=device_index,
    )


# ──────────────────────────────────────────────────────────────────────
# Colour pair IDs
# ──────────────────────────────────────────────────────────────────────

C_TITLE   = 1
C_MENU    = 2
C_SELECT  = 3
C_STATUS  = 4
C_ERROR   = 5
C_SUCCESS = 6
C_HEADER  = 7
C_DIM     = 8
C_WARN    = 9
C_ACCENT  = 10
C_INFO    = 11


def init_colours():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE,   curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(C_MENU,    curses.COLOR_WHITE,   -1)
    curses.init_pair(C_SELECT,  curses.COLOR_BLACK,   curses.COLOR_GREEN)
    curses.init_pair(C_STATUS,  curses.COLOR_CYAN,    -1)
    curses.init_pair(C_ERROR,   curses.COLOR_RED,     -1)
    curses.init_pair(C_SUCCESS, curses.COLOR_GREEN,   -1)
    curses.init_pair(C_HEADER,  curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_DIM,     curses.COLOR_WHITE,   -1)
    curses.init_pair(C_WARN,    curses.COLOR_BLACK,   curses.COLOR_YELLOW)
    curses.init_pair(C_ACCENT,  curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_INFO,    curses.COLOR_CYAN,    -1)


# ──────────────────────────────────────────────────────────────────────
# Drawing primitives
# ──────────────────────────────────────────────────────────────────────

def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, str(text)[:max_len], attr)
    except curses.error:
        pass


def draw_title_bar(stdscr, left: str, right: str = ""):
    h, w = stdscr.getmaxyx()
    l = f" {left} "
    r = f" {right} " if right else ""
    pad = " " * max(0, w - len(l) - len(r))
    try:
        stdscr.addstr(0, 0, (l + pad + r)[:w], curses.color_pair(C_TITLE) | curses.A_BOLD)
    except curses.error:
        pass


def draw_status_bar(stdscr, left: str, right: str = "", colour: int = C_DIM):
    h, w = stdscr.getmaxyx()
    r = f" {right} " if right else ""
    l = f" {left}"[: w - len(r) - 1]
    pad = " " * max(0, w - len(l) - len(r))
    try:
        stdscr.addstr(h - 1, 0, (l + pad + r)[:w], curses.color_pair(colour))
    except curses.error:
        pass


def draw_box(stdscr, y: int, x: int, h: int, w: int, title: str = "", colour: int = C_DIM):
    max_h, max_w = stdscr.getmaxyx()
    if y + h > max_h or x + w > max_w or h < 2 or w < 2:
        return
    attr = curses.color_pair(colour)
    tl, tr, bl, br, hz, vt = "┌", "┐", "└", "┘", "─", "│"
    top = tl + hz * (w - 2) + tr
    if title:
        ttl = f" {title} "
        ins = 2
        top = tl + hz * ins + ttl + hz * max(0, w - 2 - ins - len(ttl)) + tr
    try:
        stdscr.addstr(y, x, top[: max_w - x], attr)
    except curses.error:
        pass
    for row in range(1, h - 1):
        try:
            stdscr.addstr(y + row, x, vt, attr)
            stdscr.addstr(y + row, x + w - 1, vt, attr)
        except curses.error:
            pass
    bot = bl + hz * (w - 2) + br
    try:
        stdscr.addstr(y + h - 1, x, bot[: max_w - x], attr)
    except curses.error:
        pass


# ──────────────────────────────────────────────────────────────────────
# Animated spinner – runs AWS call in background thread
# ──────────────────────────────────────────────────────────────────────

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def run_with_spinner(stdscr, message: str, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a thread, showing an animated spinner.
    Raises any exception thrown by fn. Returns fn's return value."""
    result = [None]
    exc = [None]

    def _target():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    frame = 0
    while t.is_alive():
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_title_bar(stdscr, "EC2 Backup Restore", aws.region)
        spin = _SPINNER[frame % len(_SPINNER)]
        msg = f"  {spin}  {message}"
        cy = h // 2
        safe_addstr(stdscr, cy - 1, 2, "─" * max(0, w - 4), curses.color_pair(C_DIM))
        safe_addstr(stdscr, cy, max(0, (w - len(msg)) // 2), msg,
                    curses.color_pair(C_STATUS) | curses.A_BOLD)
        safe_addstr(stdscr, cy + 1, 2, "─" * max(0, w - 4), curses.color_pair(C_DIM))
        draw_status_bar(stdscr, "Please wait...")
        stdscr.refresh()
        frame += 1
        time.sleep(0.1)

    t.join()
    if exc[0] is not None:
        raise exc[0]
    return result[0]


# ──────────────────────────────────────────────────────────────────────
# Scrollable menu with search filter and optional detail pane
# ──────────────────────────────────────────────────────────────────────

def menu_select(
    stdscr,
    title: str,
    items: list,
    format_fn=None,
    detail_fn=None,
    breadcrumb: str = "",
    status_hint: str = "",
) -> int:
    """Scrollable selector with live search and optional detail panel.

    Returns the index into `items` of the chosen item, or -1 on cancel.
    Press  /  to open search, Esc to clear, Enter to lock filter.
    """
    if not items:
        return -1

    cur = 0
    offset = 0
    search_active = False
    search_query = ""
    filtered: List[int] = list(range(len(items)))

    def _apply_filter(q: str):
        nonlocal filtered, cur, offset
        if not q:
            filtered = list(range(len(items)))
        else:
            ql = q.lower()
            filtered = [
                i for i in range(len(items))
                if ql in (format_fn(items[i]) if format_fn else str(items[i])).lower()
            ]
        cur = 0
        offset = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_title_bar(stdscr, "EC2 Backup Restore", aws.region)

        row = 1
        if breadcrumb:
            safe_addstr(stdscr, row, 2, breadcrumb, curses.color_pair(C_DIM))
            row += 1

        safe_addstr(stdscr, row, 2, title, curses.color_pair(C_HEADER) | curses.A_BOLD)
        row += 1
        safe_addstr(stdscr, row, 2, "─" * min(len(title) + 4, w - 4),
                    curses.color_pair(C_DIM))
        row += 1

        # Search bar
        if search_active:
            search_label = f"  /  {search_query}█"
            safe_addstr(stdscr, row, 2, search_label, curses.color_pair(C_ACCENT))
        else:
            safe_addstr(stdscr, row, 2,
                        "  Press / to search" if items else "",
                        curses.color_pair(C_DIM))
        row += 1

        list_start = row + 1

        # Detail pane: right third of screen if wide enough
        detail_w = min(42, w // 3) if detail_fn and w > 90 else 0
        list_w = w - detail_w - 5

        visible = max(1, h - list_start - 2)

        if cur < offset:
            offset = cur
        if cur >= offset + visible:
            offset = cur - visible + 1

        for idx in range(offset, min(len(filtered), offset + visible)):
            r = list_start + idx - offset
            orig = filtered[idx]
            label = (format_fn(items[orig]) if format_fn else str(items[orig]))
            if len(label) > list_w - 3:
                label = label[: list_w - 6] + "..."
            marker = "▸ " if idx == cur else "  "
            attr = (curses.color_pair(C_SELECT) | curses.A_BOLD
                    if idx == cur else curses.color_pair(C_MENU))
            safe_addstr(stdscr, r, 3, marker + label, attr)

        # Scrollbar + count
        count_str = f" {cur + 1}/{len(filtered)} "
        safe_addstr(stdscr, list_start - 1, w - len(count_str) - 2, count_str,
                    curses.color_pair(C_DIM))
        if len(filtered) > visible:
            sb_h = max(1, visible * visible // max(len(filtered), 1))
            sb_top = (cur * (visible - sb_h)) // max(len(filtered) - 1, 1)
            for sb in range(visible):
                ch = "█" if sb_top <= sb < sb_top + sb_h else "░"
                safe_addstr(stdscr, list_start + sb, w - 2, ch,
                            curses.color_pair(C_DIM))

        # Detail pane
        if detail_fn and detail_w > 0 and filtered:
            orig = filtered[cur]
            details = detail_fn(items[orig])
            draw_box(stdscr, list_start - 1, w - detail_w - 1,
                     min(visible + 2, h - list_start + 1), detail_w, "Details")
            for di, dline in enumerate(details[: visible]):
                safe_addstr(stdscr, list_start + di, w - detail_w,
                            str(dline)[: detail_w - 2],
                            curses.color_pair(C_INFO))

        if search_active:
            hint = "Type to filter  │  Enter confirm  │  Esc clear"
        else:
            hint = status_hint or "↑↓/jk Navigate  │  Enter Select  │  / Search  │  q Back"
        draw_status_bar(stdscr, hint,
                        f"{len(filtered)}/{len(items)} shown" if search_query else "")
        stdscr.refresh()

        key = stdscr.getch()

        if key == curses.KEY_RESIZE:
            curses.resizeterm(*stdscr.getmaxyx())
            continue

        if search_active:
            if key == 27:
                search_active = False
                search_query = ""
                _apply_filter("")
            elif key in (curses.KEY_ENTER, 10, 13):
                search_active = False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                search_query = search_query[:-1]
                _apply_filter(search_query)
            elif 32 <= key < 127:
                search_query += chr(key)
                _apply_filter(search_query)
        else:
            if key in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cur = min(len(filtered) - 1, cur + 1) if filtered else 0
            elif key == curses.KEY_PPAGE:
                cur = max(0, cur - visible)
            elif key == curses.KEY_NPAGE:
                cur = min(len(filtered) - 1, cur + visible) if filtered else 0
            elif key in (curses.KEY_HOME, ord("g")):
                cur = 0
            elif key in (curses.KEY_END, ord("G")):
                cur = max(0, len(filtered) - 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                return filtered[cur] if filtered else -1
            elif key in (27, ord("q")):
                return -1
            elif key == ord("/"):
                search_active = True
                search_query = ""


# ──────────────────────────────────────────────────────────────────────
# Dialog components
# ──────────────────────────────────────────────────────────────────────

def show_message(stdscr, title: str, lines: list, colour: int = C_STATUS,
                 wait: bool = True):
    """Centred message box. Waits for keypress if wait=True."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")

    max_line = max((len(str(l)) for l in lines), default=0)
    bw = min(max(len(title) + 8, max_line + 6, 48), w - 4)
    bh = len(lines) + 5
    by = max(2, (h - bh) // 2)
    bx = max(1, (w - bw) // 2)

    icon = {"C_SUCCESS": "✓", "C_ERROR": "✗", "C_WARN": "⚠"}.get(
        {C_SUCCESS: "C_SUCCESS", C_ERROR: "C_ERROR", C_WARN: "C_WARN"}.get(colour, ""),
        "ℹ",
    )
    draw_box(stdscr, by, bx, bh, bw, f"{icon} {title}")

    for i, line in enumerate(lines):
        safe_addstr(stdscr, by + 2 + i, bx + 2, str(line)[: bw - 4],
                    curses.color_pair(colour))

    if wait:
        draw_status_bar(stdscr, "Press any key to continue...", colour=colour)
        stdscr.refresh()
        stdscr.getch()
    else:
        stdscr.refresh()


def confirm_dialog(stdscr, title: str, detail_lines: list) -> bool:
    """Returns True if user presses y/Y."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")

    max_line = max((len(l) for l in detail_lines), default=40)
    bw = min(max(len(title) + 10, max_line + 6, 56), w - 4)
    bh = len(detail_lines) + 7
    by = max(2, (h - bh) // 2)
    bx = max(1, (w - bw) // 2)

    draw_box(stdscr, by, bx, bh, bw, " ⚠  CONFIRM ", C_WARN)
    safe_addstr(stdscr, by + 1, bx + 2, title,
                curses.color_pair(C_WARN) | curses.A_BOLD)
    safe_addstr(stdscr, by + 2, bx + 1, "─" * (bw - 2), curses.color_pair(C_DIM))

    for i, line in enumerate(detail_lines):
        safe_addstr(stdscr, by + 3 + i, bx + 2, str(line)[: bw - 4],
                    curses.color_pair(C_MENU))

    py = by + 3 + len(detail_lines) + 1
    safe_addstr(stdscr, py, bx + 2, "[ y ]  Confirm      [ n ]  Cancel",
                curses.color_pair(C_ERROR) | curses.A_BOLD)
    draw_status_bar(stdscr, "y = Yes, confirm  │  n / Esc = No, cancel", colour=C_WARN)
    stdscr.refresh()

    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), ord("q"), 27):
            return False


def input_text(stdscr, prompt: str, default: str = "", hint: str = "") -> str:
    """Single-line text input. Blank input returns default."""
    curses.echo()
    curses.curs_set(1)
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")
    safe_addstr(stdscr, 3, 2, prompt, curses.color_pair(C_HEADER) | curses.A_BOLD)
    if default:
        safe_addstr(stdscr, 5, 4, f"Default:  {default}", curses.color_pair(C_DIM))
    if hint:
        safe_addstr(stdscr, 6, 4, hint, curses.color_pair(C_INFO))
    draw_box(stdscr, 8, 2, 3, w - 4)
    safe_addstr(stdscr, 9, 4, "▸ ", curses.color_pair(C_ACCENT))
    draw_status_bar(stdscr, "Type value  │  Leave blank to use default  │  Ctrl-G cancel")
    stdscr.refresh()
    try:
        val = stdscr.getstr(9, 6, w - 10).decode("utf-8", errors="replace").strip()
    except Exception:
        val = ""
    curses.noecho()
    curses.curs_set(0)
    return val if val else default


# ──────────────────────────────────────────────────────────────────────
# Restore job progress monitor
# ──────────────────────────────────────────────────────────────────────

def poll_restore_job(stdscr, job_id: str) -> Optional[Dict]:
    """Poll until complete/failed. Returns final status dict or None."""
    stdscr.nodelay(True)
    start = time.time()
    _BOUNCE = "⣾⣽⣻⢿⡿⣟⣯⣷"

    while True:
        try:
            status = get_restore_job_status(job_id)
        except Exception as e:
            stdscr.nodelay(False)
            show_message(stdscr, "Polling Error", [str(e)], C_ERROR)
            return None

        state = status.get("Status", "UNKNOWN")
        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_title_bar(stdscr, "EC2 Backup Restore", aws.region)
        safe_addstr(stdscr, 2, 2, "Restore Job Progress",
                    curses.color_pair(C_HEADER) | curses.A_BOLD)
        safe_addstr(stdscr, 3, 2, "─" * 50, curses.color_pair(C_DIM))

        draw_box(stdscr, 5, 2, 9, min(68, w - 4), " Job Status ")
        safe_addstr(stdscr, 7, 4, f"Job ID :  {job_id}", curses.color_pair(C_MENU))

        sc = (C_SUCCESS if state == "COMPLETED"
              else C_ERROR if state in ("FAILED", "ABORTED")
              else C_STATUS)
        safe_addstr(stdscr, 8, 4, "Status :  ", curses.color_pair(C_MENU))
        safe_addstr(stdscr, 8, 14, state, curses.color_pair(sc) | curses.A_BOLD)
        safe_addstr(stdscr, 9, 4, f"Elapsed:  {mins}m {secs:02d}s",
                    curses.color_pair(C_DIM))

        created_arn = status.get("CreatedResourceArn", "")
        if created_arn:
            new_iid = created_arn.split("/")[-1] if "/" in created_arn else created_arn
            safe_addstr(stdscr, 11, 4, f"New Instance: {new_iid}",
                        curses.color_pair(C_SUCCESS))

        if state in ("RUNNING", "PENDING", "CREATED"):
            frame = int(time.time() * 6) % len(_BOUNCE)
            safe_addstr(stdscr, 16, 4,
                        f"  {_BOUNCE[frame]}  Restoring … this may take several minutes",
                        curses.color_pair(C_ACCENT))
            # Bouncing indeterminate bar
            bar_w = min(52, w - 8)
            pos = int(time.time() * 3) % max(1, bar_w * 2)
            if pos >= bar_w:
                pos = bar_w * 2 - pos
            bar = "░" * pos + "████" + "░" * max(0, bar_w - pos - 4)
            safe_addstr(stdscr, 17, 4, bar[:bar_w], curses.color_pair(C_ACCENT))

        draw_status_bar(stdscr, "q = Stop watching  (job continues in background)")
        stdscr.refresh()

        if state == "COMPLETED":
            stdscr.nodelay(False)
            draw_status_bar(stdscr, "✓ Restore complete!  Press any key…", colour=C_SUCCESS)
            stdscr.refresh()
            stdscr.getch()
            return status

        if state in ("FAILED", "ABORTED"):
            stdscr.nodelay(False)
            reason = status.get("StatusMessage", "Unknown")
            show_message(stdscr, "Restore Failed",
                         [f"State:  {state}", f"Reason: {reason}"], C_ERROR)
            return status

        # Non-blocking quit check
        try:
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                stdscr.nodelay(False)
                show_message(stdscr, "Monitoring stopped", [
                    "The restore job continues running in the background.",
                    f"Job ID:  {job_id}",
                    "",
                    "Check status with:",
                    "  aws backup describe-restore-job --restore-job-id <id>",
                ], C_STATUS)
                return None
        except Exception:
            pass

        time.sleep(2)


# ──────────────────────────────────────────────────────────────────────
# Shared selectors and formatters
# ──────────────────────────────────────────────────────────────────────

def _fmt_rp(rp: Dict) -> str:
    ts = rp.get("CreationDate", "")
    ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
    arn = rp.get("ResourceArn", "")
    iid = arn.split("/")[-1] if "/" in arn else arn[-20:]
    size = rp.get("BackupSizeInBytes", 0)
    if size >= 1024 ** 3:
        size_str = f"{size // (1024**3)}GB"
    elif size:
        size_str = f"{size // (1024**2)}MB"
    else:
        size_str = "?"
    return f"{ts_str}  │  {iid:<22}  │  {size_str:>8}  │  {rp.get('Status', '?')}"


def _detail_rp(rp: Dict) -> List[str]:
    ts = rp.get("CreationDate", "")
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC") if hasattr(ts, "strftime") else str(ts)
    exp = rp.get("CalculatedLifecycle", {}).get("DeleteAt", "")
    exp_str = exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else (str(exp) or "N/A")
    size = rp.get("BackupSizeInBytes", 0)
    size_str = (f"{size // (1024**3)} GB" if size >= 1024 ** 3
                else f"{size // (1024**2)} MB" if size else "N/A")
    arn = rp.get("ResourceArn", "")
    iid = arn.split("/")[-1] if "/" in arn else ""
    return [
        f"Created : {ts_str}",
        f"Expires : {exp_str}",
        f"Size    : {size_str}",
        f"Status  : {rp.get('Status', '?')}",
        f"Type    : {rp.get('ResourceType', '?')}",
        f"Enc     : {'Yes' if rp.get('IsEncrypted') else 'No'}",
        "",
        f"Instance:",
        f"  {iid}",
    ]


def select_vault_and_recovery_point(stdscr) -> Optional[Dict]:
    """Walk user through vault → recovery point selection."""
    try:
        vaults = run_with_spinner(stdscr, "Loading backup vaults…", list_backup_vaults)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list vaults: {e}"], C_ERROR)
        return None

    if not vaults:
        show_message(stdscr, "No Vaults Found", [
            "No AWS Backup vaults found in this account/region.",
            f"Region: {aws.region}",
        ], C_ERROR)
        return None

    def fmt_vault(v: Dict) -> str:
        return f'{v["name"]:<40}  │  {v["recovery_points"]:>5} points'

    def detail_vault(v: Dict) -> List[str]:
        enc = v.get("encryption", "")
        enc_short = "AWS Managed" if not enc or "aws/backup" in enc else enc.split("/")[-1]
        return [
            f"Name   : {v['name']}",
            f"Points : {v['recovery_points']}",
            f"Enc    : {enc_short}",
        ]

    idx = menu_select(
        stdscr, "Select Backup Vault", vaults,
        format_fn=fmt_vault, detail_fn=detail_vault,
        breadcrumb="Home  ›  Select Vault",
    )
    if idx < 0:
        return None

    vault = vaults[idx]

    try:
        rps = run_with_spinner(
            stdscr, f"Loading recovery points from '{vault['name']}'…",
            list_recovery_points, vault["name"],
        )
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list recovery points: {e}"], C_ERROR)
        return None

    if not rps:
        show_message(stdscr, "No Recovery Points", [
            f"No EC2 recovery points found in vault '{vault['name']}'.",
        ], C_ERROR)
        return None

    idx = menu_select(
        stdscr,
        f"Recovery Points  ─  {vault['name']}  (newest first)",
        rps,
        format_fn=_fmt_rp,
        detail_fn=_detail_rp,
        breadcrumb=f"Home  ›  {vault['name']}  ›  Recovery Point",
    )
    if idx < 0:
        return None
    return rps[idx]


# ──────────────────────────────────────────────────────────────────────
# Restore workflows
# ──────────────────────────────────────────────────────────────────────

def workflow_replace_instance(stdscr):
    """Terminate existing EC2, restore from backup, re-attach original ENI."""
    rp = select_vault_and_recovery_point(stdscr)
    if rp is None:
        return
    rp_arn = rp["RecoveryPointArn"]

    # Pick target instance
    try:
        instances = run_with_spinner(stdscr, "Loading EC2 instances…", list_ec2_instances)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list instances: {e}"], C_ERROR)
        return

    if not instances:
        show_message(stdscr, "No Instances", ["No running/stopped instances found."], C_ERROR)
        return

    def fmt_inst(inst: Dict) -> str:
        icon = "▶" if inst["State"] == "running" else "■"
        name = inst["Name"] or "(no name)"
        return (f'{icon} {inst["InstanceId"]}  │  {name:<24}  │  '
                f'{inst["PrivateIp"]:<16}  │  {inst["InstanceType"]}')

    def detail_inst(inst: Dict) -> List[str]:
        lt = inst.get("LaunchTime", "")
        lt_str = lt.strftime("%Y-%m-%d %H:%M") if hasattr(lt, "strftime") else ""
        return [
            f"ID     : {inst['InstanceId']}",
            f"Name   : {inst['Name'] or '(none)'}",
            f"State  : {inst['State']}",
            f"Type   : {inst['InstanceType']}",
            f"Priv IP: {inst['PrivateIp']}",
            f"Pub IP : {inst['PublicIp']}",
            f"VPC    : {inst['VpcId']}",
            f"AZ     : {inst.get('AvailabilityZone', '?')}",
            f"Key    : {inst['KeyName'] or '(none)'}",
            f"Launch : {lt_str}",
        ]

    idx = menu_select(
        stdscr, "Select Instance to REPLACE  (will be terminated)",
        instances, format_fn=fmt_inst, detail_fn=detail_inst,
        breadcrumb="Home  ›  Vault  ›  Recovery Point  ›  Target Instance",
    )
    if idx < 0:
        return
    target = instances[idx]

    primary_eni = next(
        (ni["NetworkInterfaceId"]
         for ni in target["NetworkInterfaces"]
         if ni.get("Attachment", {}).get("DeviceIndex") == 0),
        None,
    )

    try:
        metadata = run_with_spinner(
            stdscr, "Fetching restore metadata…", fetch_restore_metadata, rp_arn)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to get metadata: {e}"], C_ERROR)
        return

    iam_role = input_text(
        stdscr, "IAM Role ARN for restore:", get_default_restore_role_arn(),
        hint="Role must have AWS Backup restore permissions",
    )
    if not iam_role:
        return

    metadata["SubnetId"] = target["SubnetId"]
    metadata["SecurityGroupIds"] = json.dumps(target["SecurityGroups"])
    metadata["InstanceType"] = target["InstanceType"]
    if target["IamInstanceProfile"]:
        metadata["IamInstanceProfileName"] = target["IamInstanceProfile"].split("/")[-1]
    if target["KeyName"]:
        metadata["KeyName"] = target["KeyName"]

    detail = [
        f"Recovery Point : {rp_arn[-55:]}",
        f"Replace        : {target['InstanceId']}  ({target['Name'] or 'no name'})",
        f"Private IP     : {target['PrivateIp']}",
        f"Public IP      : {target['PublicIp']}",
        f"Primary ENI    : {primary_eni or 'N/A'}",
        f"Subnet         : {target['SubnetId']}",
        f"Instance Type  : {target['InstanceType']}",
        "",
        "⚠  The existing instance will be TERMINATED.",
        "   The original ENI will be re-attached to preserve the IP.",
    ]
    if not confirm_dialog(stdscr, "CONFIRM INSTANCE REPLACEMENT", detail):
        return

    # Terminate
    try:
        run_with_spinner(stdscr, f"Terminating {target['InstanceId']}…",
                         terminate_instance, target["InstanceId"])
    except Exception as e:
        show_message(stdscr, "Error",
                     [f"Failed to terminate: {e}", "Aborting restore."], C_ERROR)
        return

    try:
        run_with_spinner(stdscr, "Waiting for instance to terminate…",
                         wait_for_instance_terminated, target["InstanceId"])
    except Exception as e:
        show_message(stdscr, "Warning",
                     [f"Termination wait error: {e}", "Continuing…"], C_WARN)

    if primary_eni:
        try:
            run_with_spinner(stdscr,
                             f"Waiting for ENI {primary_eni} to become available…",
                             wait_for_eni_available, primary_eni, 120)
        except Exception as e:
            show_message(stdscr, "Warning",
                         [f"ENI wait timed out: {e}", "Will attempt restore anyway…"],
                         C_WARN)

    try:
        job_id = run_with_spinner(stdscr, "Starting restore job…",
                                  start_restore_job, rp_arn, metadata, iam_role)
    except Exception as e:
        show_message(stdscr, "Restore Failed", [f"Error: {e}"], C_ERROR)
        return

    _restore_jobs.append({
        "id": job_id,
        "started": datetime.now().strftime("%H:%M:%S"),
        "mode": "replace",
        "status": "RUNNING",
    })

    result = poll_restore_job(stdscr, job_id)
    if result:
        _restore_jobs[-1]["status"] = result.get("Status", "?")

    if result and result.get("Status") == "COMPLETED" and primary_eni:
        created_arn = result.get("CreatedResourceArn", "")
        new_iid = created_arn.split("/")[-1] if "/" in created_arn else ""
        if new_iid:
            try:
                run_with_spinner(stdscr, f"Attaching ENI {primary_eni} to {new_iid}…",
                                 attach_eni, primary_eni, new_iid, 1)
                show_message(stdscr, "Restore Complete!", [
                    f"New instance : {new_iid}",
                    f"Attached ENI : {primary_eni}",
                    f"Original IP  : {target['PrivateIp']}",
                    "",
                    "ENI attached as secondary interface (device index 1).",
                    "You may need OS-level routing for the secondary ENI.",
                ], C_SUCCESS)
            except Exception as e:
                show_message(stdscr, "ENI Attach Failed", [
                    f"Instance restored : {new_iid}",
                    f"Attach failed     : {e}",
                    f"Manually attach   : {primary_eni}",
                ], C_ERROR)
        else:
            show_message(stdscr, "Restore Complete", [
                "Job completed but could not determine new instance ID.",
                f"Job ID: {job_id}",
                "Check the AWS Console for the restored instance.",
            ], C_STATUS)


def workflow_new_instance_with_eni(stdscr):
    """Restore to new EC2 instance and attach a pre-created available ENI."""
    rp = select_vault_and_recovery_point(stdscr)
    if rp is None:
        return
    rp_arn = rp["RecoveryPointArn"]

    try:
        enis = run_with_spinner(stdscr, "Loading available ENIs…", list_enis)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list ENIs: {e}"], C_ERROR)
        return

    if not enis:
        show_message(stdscr, "No Available ENIs", [
            "No ENIs with status 'available' found.",
            "Create an ENI first, or use Option 1 instead.",
        ], C_ERROR)
        return

    def fmt_eni(eni: Dict) -> str:
        name = eni["Name"] or "(no name)"
        return (f'{eni["NetworkInterfaceId"]}  │  {name:<20}  │  '
                f'{eni["PrivateIp"]:<16}  │  {eni["SubnetId"]}')

    def detail_eni(eni: Dict) -> List[str]:
        desc = eni["Description"][:28] if eni["Description"] else "(none)"
        return [
            f"ID     : {eni['NetworkInterfaceId']}",
            f"Name   : {eni['Name'] or '(none)'}",
            f"IP     : {eni['PrivateIp']}",
            f"Subnet : {eni['SubnetId']}",
            f"VPC    : {eni['VpcId']}",
            f"AZ     : {eni.get('AvailabilityZone', '?')}",
            f"Desc   : {desc}",
        ]

    idx = menu_select(
        stdscr, "Select ENI to attach to restored instance",
        enis, format_fn=fmt_eni, detail_fn=detail_eni,
        breadcrumb="Home  ›  Vault  ›  Recovery Point  ›  Select ENI",
    )
    if idx < 0:
        return
    chosen_eni = enis[idx]

    try:
        metadata = run_with_spinner(
            stdscr, "Fetching restore metadata…", fetch_restore_metadata, rp_arn)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to get metadata: {e}"], C_ERROR)
        return

    iam_role = input_text(
        stdscr, "IAM Role ARN for restore:", get_default_restore_role_arn(),
        hint="Role must have AWS Backup restore permissions",
    )
    if not iam_role:
        return

    current_type = metadata.get("InstanceType", "t3.medium")
    inst_type = input_text(
        stdscr, "Instance type:", current_type,
        hint=f"Backup was originally: {current_type}",
    )

    metadata["SubnetId"] = chosen_eni["SubnetId"]
    metadata["SecurityGroupIds"] = json.dumps(chosen_eni["SecurityGroups"])
    metadata["InstanceType"] = inst_type

    detail = [
        f"Recovery Point : {rp_arn[-55:]}",
        f"Attach ENI     : {chosen_eni['NetworkInterfaceId']}",
        f"ENI Name       : {chosen_eni['Name'] or '(none)'}",
        f"ENI Private IP : {chosen_eni['PrivateIp']}",
        f"Subnet         : {chosen_eni['SubnetId']}",
        f"Instance Type  : {inst_type}",
        "",
        "A new EC2 instance will be restored from backup.",
        "The selected ENI will be attached after restore completes.",
    ]
    if not confirm_dialog(stdscr, "CONFIRM NEW INSTANCE RESTORE", detail):
        return

    try:
        job_id = run_with_spinner(stdscr, "Starting restore job…",
                                  start_restore_job, rp_arn, metadata, iam_role)
    except Exception as e:
        show_message(stdscr, "Restore Failed", [f"Error: {e}"], C_ERROR)
        return

    _restore_jobs.append({
        "id": job_id,
        "started": datetime.now().strftime("%H:%M:%S"),
        "mode": "new+eni",
        "status": "RUNNING",
    })

    result = poll_restore_job(stdscr, job_id)
    if result:
        _restore_jobs[-1]["status"] = result.get("Status", "?")

    if result and result.get("Status") == "COMPLETED":
        created_arn = result.get("CreatedResourceArn", "")
        new_iid = created_arn.split("/")[-1] if "/" in created_arn else ""
        if new_iid:
            try:
                run_with_spinner(
                    stdscr, f"Attaching ENI {chosen_eni['NetworkInterfaceId']}…",
                    attach_eni, chosen_eni["NetworkInterfaceId"], new_iid, 1,
                )
                show_message(stdscr, "Restore Complete!", [
                    f"New instance : {new_iid}",
                    f"Attached ENI : {chosen_eni['NetworkInterfaceId']}",
                    f"ENI IP       : {chosen_eni['PrivateIp']}",
                    "",
                    "ENI attached as secondary interface (device index 1).",
                    "Configure OS networking if needed.",
                ], C_SUCCESS)
            except Exception as e:
                show_message(stdscr, "ENI Attach Failed", [
                    f"Instance restored : {new_iid}",
                    f"Attach failed     : {e}",
                    f"Manually attach   : {chosen_eni['NetworkInterfaceId']}",
                ], C_ERROR)
        else:
            show_message(stdscr, "Restore Complete", [
                "Job completed but could not extract instance ID.",
                f"Job ID: {job_id}",
                "Check the AWS Console for the restored instance.",
            ], C_STATUS)


# ──────────────────────────────────────────────────────────────────────
# Help and session job history
# ──────────────────────────────────────────────────────────────────────

def show_help(stdscr):
    lines = [
        "Navigation",
        "  ↑ / k       Move up",
        "  ↓ / j       Move down",
        "  PgUp/PgDn   Scroll one page",
        "  g / Home    Jump to first item",
        "  G / End     Jump to last item",
        "  /           Open search / filter",
        "  Enter       Select highlighted item",
        "  q / Esc     Go back or cancel",
        "",
        "Main menu",
        "  1   Replace existing instance",
        "  2   Restore new instance + ENI",
        "  3   View session job history",
        "  ?   Show this help screen",
        "  q   Quit",
        "",
        "Restore monitor",
        "  q   Stop watching (job continues in background)",
    ]
    show_message(stdscr, "Keyboard Shortcuts", lines, C_INFO)


def show_session_jobs(stdscr):
    if not _restore_jobs:
        show_message(stdscr, "Session Jobs", ["No restore jobs started this session."], C_DIM)
        return
    lines = [
        f"{j['started']}  │  {j['id']}  │  {j.get('mode','?'):>8}  │  {j.get('status','?')}"
        for j in _restore_jobs
    ]
    show_message(stdscr, f"Session Restore Jobs  ({len(_restore_jobs)})", lines, C_INFO)


# ──────────────────────────────────────────────────────────────────────
# Main menu
# ──────────────────────────────────────────────────────────────────────

MAIN_MENU = [
    ("1", "Replace Existing Instance",
     "Terminate current EC2, restore from backup, re-attach ENI to preserve IP"),
    ("2", "Restore New Instance + ENI",
     "Restore to a brand-new EC2 and attach a pre-created available ENI"),
    ("3", "Session Job History",
     "View restore jobs started in this session"),
    ("?", "Help / Keyboard Shortcuts",
     "Show navigation and shortcut reference"),
    ("q", "Quit", ""),
]


def draw_main_menu(stdscr, selected: int, account_id: str):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore  ─  AWS CloudShell", aws.region)

    logo = [
        "  ╔═══════════════════════════════════════════════════════╗",
        "  ║        EC2  AWS  BACKUP  RESTORE  UTILITY            ║",
        "  ╚═══════════════════════════════════════════════════════╝",
    ]
    for i, line in enumerate(logo):
        safe_addstr(stdscr, 2 + i, 2, line, curses.color_pair(C_ACCENT) | curses.A_BOLD)

    safe_addstr(stdscr, 6, 4, f"Account : {account_id}", curses.color_pair(C_DIM))
    safe_addstr(stdscr, 7, 4, f"Region  : {aws.region}", curses.color_pair(C_DIM))
    if _restore_jobs:
        safe_addstr(stdscr, 8, 4,
                    f"Jobs    : {len(_restore_jobs)} restore job(s) this session",
                    curses.color_pair(C_INFO))

    menu_top = 10
    bh = len(MAIN_MENU) * 3 + 3
    bw = min(74, w - 6)
    draw_box(stdscr, menu_top, 3, bh, bw, " Select Operation ")

    for i, (key, label, desc) in enumerate(MAIN_MENU):
        row = menu_top + 2 + i * 3
        is_sel = i == selected
        prefix = " ▸ " if is_sel else "   "
        attr = (curses.color_pair(C_SELECT) | curses.A_BOLD
                if is_sel else curses.color_pair(C_MENU))
        safe_addstr(stdscr, row, 5, f"{prefix}[{key}]  {label}", attr)
        if desc:
            safe_addstr(stdscr, row + 1, 12, desc, curses.color_pair(C_DIM))

    draw_status_bar(stdscr,
                    "↑/↓ Navigate  │  Enter Select  │  1/2/3 Shortcut  │  ? Help  │  q Quit")
    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    init_colours()
    stdscr.timeout(-1)

    # Handle terminal resize gracefully
    def _sigwinch(sig, frame):
        curses.resizeterm(*stdscr.getmaxyx())
    try:
        signal.signal(signal.SIGWINCH, _sigwinch)
    except (AttributeError, OSError):
        pass

    # Verify AWS credentials with animated spinner
    try:
        account_id = run_with_spinner(stdscr, "Connecting to AWS…", aws.get_account_id)
    except NoCredentialsError:
        show_message(stdscr, "No AWS Credentials", [
            "Could not find AWS credentials.",
            "Run this tool inside AWS CloudShell, or configure",
            "credentials via environment variables / ~/.aws/credentials.",
        ], C_ERROR)
        return
    except Exception as e:
        show_message(stdscr, "AWS Connection Error", [f"Cannot connect to AWS: {e}"], C_ERROR)
        return

    selected = 0
    while True:
        draw_main_menu(stdscr, selected, account_id)
        key = stdscr.getch()

        if key == curses.KEY_RESIZE:
            curses.resizeterm(*stdscr.getmaxyx())
            continue

        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(MAIN_MENU)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(MAIN_MENU)
        elif key in (curses.KEY_ENTER, 10, 13):
            choice = MAIN_MENU[selected][0]
            if choice == "1":
                workflow_replace_instance(stdscr)
            elif choice == "2":
                workflow_new_instance_with_eni(stdscr)
            elif choice == "3":
                show_session_jobs(stdscr)
            elif choice == "?":
                show_help(stdscr)
            elif choice == "q":
                break
        elif key == ord("1"):
            workflow_replace_instance(stdscr)
        elif key == ord("2"):
            workflow_new_instance_with_eni(stdscr)
        elif key == ord("3"):
            show_session_jobs(stdscr)
        elif key == ord("?"):
            show_help(stdscr)
        elif key in (ord("q"), 27):
            break


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("\nExiting.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
