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
Usage:        python3 ec2_restore_tui.py
"""

import curses
import json
import sys
import time
import textwrap
from datetime import datetime, timezone
from typing import Optional

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

    @property
    def region(self):
        return self._region

    @property
    def ec2(self):
        if self._ec2 is None:
            self._ec2 = self._session.client("ec2", region_name=self._region)
        return self._ec2

    @property
    def ec2_resource(self):
        return self._session.resource("ec2", region_name=self._region)

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
        return self.sts.get_caller_identity()["Account"]


aws = AWSClients()


# ──────────────────────────────────────────────────────────────────────
# Data-fetching functions
# ──────────────────────────────────────────────────────────────────────

def list_backup_vaults():
    """Return list of backup vault names."""
    vaults = []
    paginator = aws.backup.get_paginator("list_backup_vaults")
    for page in paginator.paginate():
        for v in page.get("BackupVaultList", []):
            vaults.append(v["BackupVaultName"])
    return sorted(vaults)


def list_recovery_points(vault_name: str):
    """Return EC2 recovery points from a vault, newest first."""
    points = []
    paginator = aws.backup.get_paginator("list_recovery_points_by_backup_vault")
    for page in paginator.paginate(BackupVaultName=vault_name):
        for rp in page.get("RecoveryPoints", []):
            arn = rp.get("ResourceArn", "")
            rtype = rp.get("ResourceType", "")
            if rtype == "EC2" or ":instance/" in arn:
                points.append(rp)
    points.sort(key=lambda r: r.get("CreationDate", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return points


def list_ec2_instances():
    """Return running/stopped EC2 instances."""
    instances = []
    paginator = aws.ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                name = ""
                for tag in inst.get("Tags", []):
                    if tag["Key"] == "Name":
                        name = tag["Value"]
                        break
                instances.append({
                    "InstanceId": inst["InstanceId"],
                    "Name": name,
                    "State": inst["State"]["Name"],
                    "InstanceType": inst.get("InstanceType", ""),
                    "PrivateIp": inst.get("PrivateIpAddress", "N/A"),
                    "PublicIp": inst.get("PublicIpAddress", "N/A"),
                    "SubnetId": inst.get("SubnetId", ""),
                    "VpcId": inst.get("VpcId", ""),
                    "NetworkInterfaces": inst.get("NetworkInterfaces", []),
                    "SecurityGroups": [sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
                    "IamInstanceProfile": inst.get("IamInstanceProfile", {}).get("Arn", ""),
                    "KeyName": inst.get("KeyName", ""),
                })
    return instances


def list_enis():
    """Return available (not in-use) ENIs."""
    enis = []
    paginator = aws.ec2.get_paginator("describe_network_interfaces")
    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        for eni in page["NetworkInterfaces"]:
            name = ""
            for tag in eni.get("TagSet", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
                    break
            enis.append({
                "NetworkInterfaceId": eni["NetworkInterfaceId"],
                "Name": name,
                "SubnetId": eni.get("SubnetId", ""),
                "VpcId": eni.get("VpcId", ""),
                "PrivateIp": eni.get("PrivateIpAddress", "N/A"),
                "Description": eni.get("Description", ""),
                "SecurityGroups": [sg["GroupId"] for sg in eni.get("Groups", [])],
            })
    return enis


def get_iam_role_arn_for_restore():
    """Build the default AWS Backup restore role ARN."""
    account_id = aws.get_account_id()
    return f"arn:aws:iam::{account_id}:role/service-role/AWSBackupDefaultServiceRole"


def start_restore_job(recovery_point_arn: str, metadata: dict, iam_role_arn: str) -> str:
    """Start an AWS Backup restore job and return the job ID."""
    resp = aws.backup.start_restore_job(
        RecoveryPointArn=recovery_point_arn,
        Metadata=metadata,
        IamRoleArn=iam_role_arn,
        ResourceType="EC2",
    )
    return resp["RestoreJobId"]


def get_restore_job_status(job_id: str) -> dict:
    """Poll an AWS Backup restore job."""
    return aws.backup.describe_restore_job(RestoreJobId=job_id)


def terminate_instance(instance_id: str):
    """Terminate an EC2 instance."""
    aws.ec2.terminate_instances(InstanceIds=[instance_id])


def detach_eni(eni_id: str, instance_id: str):
    """Detach an ENI from an instance."""
    resp = aws.ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
    for eni in resp["NetworkInterfaces"]:
        attachment = eni.get("Attachment")
        if attachment and attachment.get("InstanceId") == instance_id:
            aws.ec2.detach_network_interface(
                AttachmentId=attachment["AttachmentId"], Force=True
            )
            return True
    return False


def wait_for_eni_available(eni_id: str, timeout: int = 300):
    """Wait until ENI status is 'available'."""
    waiter = aws.ec2.get_waiter("network_interface_available")
    waiter.wait(
        NetworkInterfaceIds=[eni_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": timeout // 5},
    )


def attach_eni(eni_id: str, instance_id: str, device_index: int = 1):
    """Attach ENI to an instance."""
    aws.ec2.attach_network_interface(
        NetworkInterfaceId=eni_id,
        InstanceId=instance_id,
        DeviceIndex=device_index,
    )


def get_backup_metadata(recovery_point_arn: str) -> dict:
    """Get the restore metadata for a recovery point."""
    resp = aws.backup.get_recovery_point_restore_metadata(
        BackupVaultName=recovery_point_arn.split(":")[6].split("/")[0]
        if ":backup-vault:" in recovery_point_arn
        else recovery_point_arn.split(":")[-1].split("/")[0],
        RecoveryPointArn=recovery_point_arn,
    )
    return resp.get("RestoreMetadata", {})


# ──────────────────────────────────────────────────────────────────────
# Curses TUI
# ──────────────────────────────────────────────────────────────────────

# Colour pair IDs
C_TITLE = 1
C_MENU = 2
C_SELECT = 3
C_STATUS = 4
C_ERROR = 5
C_SUCCESS = 6
C_HEADER = 7
C_DIM = 8
C_WARN = 9
C_ACCENT = 10


def init_colours():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_MENU, curses.COLOR_WHITE, -1)
    curses.init_pair(C_SELECT, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_STATUS, curses.COLOR_CYAN, -1)
    curses.init_pair(C_ERROR, curses.COLOR_RED, -1)
    curses.init_pair(C_SUCCESS, curses.COLOR_GREEN, -1)
    curses.init_pair(C_HEADER, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_WARN, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, -1)


def draw_title_bar(stdscr, text: str):
    h, w = stdscr.getmaxyx()
    title = f" {text} "
    pad = " " * max(0, w - len(title))
    try:
        stdscr.addstr(0, 0, (title + pad)[:w], curses.color_pair(C_TITLE) | curses.A_BOLD)
    except curses.error:
        pass


def draw_status_bar(stdscr, text: str, colour=C_STATUS):
    h, w = stdscr.getmaxyx()
    pad = " " * max(0, w - len(text) - 1)
    try:
        stdscr.addstr(h - 1, 0, (" " + text + pad)[:w], curses.color_pair(colour))
    except curses.error:
        pass


def draw_box(stdscr, y: int, x: int, h: int, w: int, title: str = ""):
    """Draw a bordered box with optional title."""
    max_h, max_w = stdscr.getmaxyx()
    if y + h > max_h or x + w > max_w:
        return
    # Corners and edges using unicode
    tl, tr, bl, br = "┌", "┐", "└", "┘"
    hz, vt = "─", "│"
    # Top border
    top = tl + hz * (w - 2) + tr
    if title:
        ttl = f" {title} "
        ins = 2
        top = tl + hz * ins + ttl + hz * max(0, w - 2 - ins - len(ttl)) + tr
    try:
        stdscr.addstr(y, x, top[:max_w - x], curses.color_pair(C_DIM))
    except curses.error:
        pass
    # Sides
    for row in range(1, h - 1):
        try:
            stdscr.addstr(y + row, x, vt, curses.color_pair(C_DIM))
            stdscr.addstr(y + row, x + w - 1, vt, curses.color_pair(C_DIM))
        except curses.error:
            pass
    # Bottom border
    bot = bl + hz * (w - 2) + br
    try:
        stdscr.addstr(y + h - 1, x, bot[:max_w - x], curses.color_pair(C_DIM))
    except curses.error:
        pass


def safe_addstr(stdscr, y, x, text, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


def menu_select(stdscr, title: str, items: list, format_fn=None, status_hint: str = ""):
    """Generic scrollable menu selector. Returns index or -1 for ESC/q."""
    if not items:
        return -1
    cur = 0
    offset = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_title_bar(stdscr, "EC2 Backup Restore")

        safe_addstr(stdscr, 2, 2, title, curses.color_pair(C_HEADER) | curses.A_BOLD)
        safe_addstr(stdscr, 3, 2, "─" * min(len(title) + 4, w - 4), curses.color_pair(C_DIM))

        visible = h - 8  # rows available for items
        if cur < offset:
            offset = cur
        if cur >= offset + visible:
            offset = cur - visible + 1

        for idx in range(offset, min(len(items), offset + visible)):
            row = 5 + idx - offset
            marker = "▸ " if idx == cur else "  "
            if format_fn:
                label = format_fn(items[idx])
            else:
                label = str(items[idx])
            label = marker + label
            attr = curses.color_pair(C_SELECT) | curses.A_BOLD if idx == cur else curses.color_pair(C_MENU)
            safe_addstr(stdscr, row, 3, label, attr)

        # Scrollbar indicator
        if len(items) > visible:
            pct = f" [{cur + 1}/{len(items)}] "
            safe_addstr(stdscr, 4, w - len(pct) - 2, pct, curses.color_pair(C_DIM))

        hint = status_hint or "↑/↓ Navigate  │  Enter Select  │  q Back"
        draw_status_bar(stdscr, hint)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cur = max(0, cur - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cur = min(len(items) - 1, cur + 1)
        elif key in (curses.KEY_PPAGE,):
            cur = max(0, cur - visible)
        elif key in (curses.KEY_NPAGE,):
            cur = min(len(items) - 1, cur + visible)
        elif key in (curses.KEY_HOME, ord("g")):
            cur = 0
        elif key in (curses.KEY_END, ord("G")):
            cur = len(items) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return cur
        elif key in (27, ord("q")):
            return -1


def show_message(stdscr, title: str, lines: list, colour=C_STATUS, wait: bool = True):
    """Show a message box and optionally wait for keypress."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")
    safe_addstr(stdscr, 2, 2, title, curses.color_pair(C_HEADER) | curses.A_BOLD)
    for i, line in enumerate(lines):
        safe_addstr(stdscr, 4 + i, 4, line, curses.color_pair(colour))
    if wait:
        draw_status_bar(stdscr, "Press any key to continue...")
        stdscr.refresh()
        stdscr.getch()
    else:
        stdscr.refresh()


def confirm_dialog(stdscr, title: str, detail_lines: list) -> bool:
    """Show a confirmation dialog. Returns True if user presses 'y'."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")

    safe_addstr(stdscr, 2, 2, "⚠  " + title, curses.color_pair(C_WARN) | curses.A_BOLD)
    safe_addstr(stdscr, 3, 2, "─" * min(60, w - 4), curses.color_pair(C_DIM))

    for i, line in enumerate(detail_lines):
        safe_addstr(stdscr, 5 + i, 4, line, curses.color_pair(C_MENU))

    prompt_y = 6 + len(detail_lines)
    safe_addstr(stdscr, prompt_y, 4, "Proceed? (y/n) ", curses.color_pair(C_ERROR) | curses.A_BOLD)
    draw_status_bar(stdscr, "y = Yes, confirm  │  n/q = No, cancel")
    stdscr.refresh()

    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), ord("q"), 27):
            return False


def show_loading(stdscr, message: str):
    """Show a non-blocking loading message."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    frame = int(time.time() * 8) % len(spinner)
    safe_addstr(stdscr, h // 2, (w - len(message) - 4) // 2,
                f" {spinner[frame]}  {message} ", curses.color_pair(C_STATUS))
    stdscr.refresh()


def input_text(stdscr, prompt: str, default: str = "") -> str:
    """Simple single-line text input. Returns string or empty on ESC."""
    curses.echo()
    curses.curs_set(1)
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore")
    safe_addstr(stdscr, 3, 2, prompt, curses.color_pair(C_HEADER) | curses.A_BOLD)
    if default:
        safe_addstr(stdscr, 5, 4, f"Default: {default}", curses.color_pair(C_DIM))
    safe_addstr(stdscr, 7, 4, "▸ ", curses.color_pair(C_ACCENT))
    draw_status_bar(stdscr, "Enter value  │  Leave blank for default  │  Ctrl-G cancel")
    stdscr.refresh()
    try:
        val = stdscr.getstr(7, 6, w - 10).decode("utf-8", errors="replace").strip()
    except Exception:
        val = ""
    curses.noecho()
    curses.curs_set(0)
    return val if val else default


def poll_restore_job(stdscr, job_id: str):
    """Poll restore job until completion, showing progress."""
    stdscr.nodelay(True)
    start = time.time()
    while True:
        try:
            status = get_restore_job_status(job_id)
        except Exception as e:
            show_message(stdscr, "Error polling job", [str(e)], C_ERROR)
            stdscr.nodelay(False)
            return None

        state = status.get("Status", "UNKNOWN")
        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        draw_title_bar(stdscr, "EC2 Backup Restore")

        safe_addstr(stdscr, 2, 2, "Restore Job Progress", curses.color_pair(C_HEADER) | curses.A_BOLD)
        safe_addstr(stdscr, 3, 2, "─" * 40, curses.color_pair(C_DIM))

        safe_addstr(stdscr, 5, 4, f"Job ID:   {job_id}", curses.color_pair(C_MENU))
        state_colour = C_SUCCESS if state == "COMPLETED" else C_ERROR if state == "FAILED" else C_STATUS
        safe_addstr(stdscr, 6, 4, f"Status:   {state}", curses.color_pair(state_colour) | curses.A_BOLD)
        safe_addstr(stdscr, 7, 4, f"Elapsed:  {mins}m {secs:02d}s", curses.color_pair(C_DIM))

        created_id = status.get("CreatedResourceArn", "")
        if created_id:
            safe_addstr(stdscr, 9, 4, f"Created:  {created_id}", curses.color_pair(C_SUCCESS))

        # Spinner
        spinner = "⣾⣽⣻⢿⡿⣟⣯⣷"
        frame = int(time.time() * 6) % len(spinner)
        if state in ("RUNNING", "PENDING", "CREATED"):
            safe_addstr(stdscr, 11, 4, f"  {spinner[frame]}  Restoring... this may take several minutes",
                        curses.color_pair(C_ACCENT))

        draw_status_bar(stdscr, "q = Stop watching (job continues in background)")
        stdscr.refresh()

        if state == "COMPLETED":
            stdscr.nodelay(False)
            draw_status_bar(stdscr, "✓ Restore complete! Press any key...", C_SUCCESS)
            stdscr.refresh()
            stdscr.getch()
            return status

        if state in ("FAILED", "ABORTED"):
            reason = status.get("StatusMessage", "Unknown reason")
            stdscr.nodelay(False)
            show_message(stdscr, "Restore Failed", [f"Reason: {reason}"], C_ERROR)
            return status

        # Check for user quit
        try:
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                stdscr.nodelay(False)
                show_message(stdscr, "Monitoring stopped",
                             ["The restore job continues in the background.",
                              f"Job ID: {job_id}",
                              "Check with: aws backup describe-restore-job"],
                             C_STATUS)
                return None
        except Exception:
            pass

        time.sleep(3)


# ──────────────────────────────────────────────────────────────────────
# Restore Workflows
# ──────────────────────────────────────────────────────────────────────

def select_vault_and_recovery_point(stdscr):
    """Guide user through vault → recovery point selection. Returns RP or None."""
    show_loading(stdscr, "Loading backup vaults...")
    try:
        vaults = list_backup_vaults()
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list vaults: {e}"], C_ERROR)
        return None

    if not vaults:
        show_message(stdscr, "No Vaults Found",
                     ["No AWS Backup vaults found in this account/region.",
                      f"Region: {aws.region}"], C_ERROR)
        return None

    idx = menu_select(stdscr, "Select Backup Vault", vaults,
                      status_hint="↑/↓ Navigate  │  Enter Select  │  q Back")
    if idx < 0:
        return None

    vault = vaults[idx]
    show_loading(stdscr, f"Loading recovery points from '{vault}'...")
    try:
        rps = list_recovery_points(vault)
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list recovery points: {e}"], C_ERROR)
        return None

    if not rps:
        show_message(stdscr, "No Recovery Points",
                     [f"No EC2 recovery points found in vault '{vault}'."], C_ERROR)
        return None

    def fmt_rp(rp):
        ts = rp.get("CreationDate", "")
        if hasattr(ts, "strftime"):
            ts = ts.strftime("%Y-%m-%d %H:%M UTC")
        arn = rp.get("ResourceArn", "")
        iid = arn.split("/")[-1] if "/" in arn else arn[-20:]
        status = rp.get("Status", "?")
        return f"{ts}  │  {iid}  │  {status}"

    idx = menu_select(stdscr, f"Recovery Points in '{vault}'  (newest first)", rps, fmt_rp)
    if idx < 0:
        return None
    return rps[idx]


def workflow_replace_instance(stdscr):
    """Option 1: Restore over existing instance, preserving IP."""
    # Step 1 – pick recovery point
    rp = select_vault_and_recovery_point(stdscr)
    if rp is None:
        return

    rp_arn = rp["RecoveryPointArn"]

    # Step 2 – pick target instance to replace
    show_loading(stdscr, "Loading EC2 instances...")
    try:
        instances = list_ec2_instances()
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list instances: {e}"], C_ERROR)
        return

    if not instances:
        show_message(stdscr, "No Instances", ["No running/stopped instances found."], C_ERROR)
        return

    def fmt_inst(inst):
        name = inst["Name"] or "(no name)"
        return f'{inst["InstanceId"]}  │  {name}  │  {inst["PrivateIp"]}  │  {inst["State"]}'

    idx = menu_select(stdscr, "Select instance to REPLACE (will be terminated)", instances, fmt_inst,
                      status_hint="↑/↓ Navigate  │  Enter Select  │  q Back")
    if idx < 0:
        return
    target = instances[idx]

    # Capture network info
    primary_eni = None
    for ni in target["NetworkInterfaces"]:
        if ni.get("Attachment", {}).get("DeviceIndex") == 0:
            primary_eni = ni["NetworkInterfaceId"]
            break

    # Step 3 – get restore metadata
    show_loading(stdscr, "Fetching restore metadata...")
    try:
        vault_name = rp_arn.split(":")[-1].split("/")[0] if ":backup-vault:" not in rp_arn else \
            rp_arn.split(":backup-vault:")[-1].split("/")[0]
        meta_resp = aws.backup.get_recovery_point_restore_metadata(
            BackupVaultName=vault_name,
            RecoveryPointArn=rp_arn,
        )
        metadata = meta_resp.get("RestoreMetadata", {})
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to get metadata: {e}"], C_ERROR)
        return

    # Step 4 – choose IAM role
    default_role = get_iam_role_arn_for_restore()
    iam_role = input_text(stdscr, "IAM Role ARN for restore:", default_role)
    if not iam_role:
        return

    # Step 5 – build restore metadata
    metadata["SubnetId"] = target["SubnetId"]
    metadata["SecurityGroupIds"] = json.dumps(target["SecurityGroups"])
    metadata["InstanceType"] = target["InstanceType"]
    if target["IamInstanceProfile"]:
        metadata["IamInstanceProfileName"] = target["IamInstanceProfile"].split("/")[-1]
    if target["KeyName"]:
        metadata["KeyName"] = target["KeyName"]
    # We do NOT set NetworkInterfaceId here – we'll attach after restore

    # Step 6 – confirm
    detail = [
        f"Recovery Point:  {rp_arn[-60:]}",
        f"Replace:         {target['InstanceId']}  ({target['Name']})",
        f"Private IP:      {target['PrivateIp']}",
        f"Public IP:       {target['PublicIp']}",
        f"Primary ENI:     {primary_eni or 'N/A'}",
        f"Subnet:          {target['SubnetId']}",
        f"Instance Type:   {target['InstanceType']}",
        "",
        "This will TERMINATE the existing instance,",
        "then restore from backup and attach the original ENI",
        "to preserve the existing IP address.",
    ]
    if not confirm_dialog(stdscr, "CONFIRM INSTANCE REPLACEMENT", detail):
        return

    # Step 7 – detach ENI from existing instance
    if primary_eni:
        show_loading(stdscr, f"Detaching ENI {primary_eni}...")
        try:
            # For primary ENI (index 0), we need to terminate the instance
            # as primary ENIs can't be detached while instance is running
            pass  # We'll terminate first, then the ENI becomes available
        except Exception as e:
            show_message(stdscr, "Warning", [f"ENI detach note: {e}"], C_ERROR)

    # Step 8 – terminate existing instance
    show_loading(stdscr, f"Terminating {target['InstanceId']}...")
    try:
        terminate_instance(target["InstanceId"])
        # Wait for termination
        show_loading(stdscr, "Waiting for instance termination...")
        waiter = aws.ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=[target["InstanceId"]], WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to terminate: {e}",
                                        "Aborting restore to avoid conflicts."], C_ERROR)
        return

    # Step 9 – wait for ENI to become available (primary ENI released on termination)
    if primary_eni:
        show_loading(stdscr, f"Waiting for ENI {primary_eni} to become available...")
        try:
            wait_for_eni_available(primary_eni, timeout=120)
        except Exception as e:
            show_message(stdscr, "Warning",
                         [f"ENI wait timed out: {e}",
                          "Will attempt restore anyway..."], C_WARN)

    # Step 10 – kick off restore
    show_loading(stdscr, "Starting restore job...")
    try:
        job_id = start_restore_job(rp_arn, metadata, iam_role)
    except Exception as e:
        show_message(stdscr, "Restore Failed", [f"Error: {e}"], C_ERROR)
        return

    # Step 11 – poll
    result = poll_restore_job(stdscr, job_id)

    if result and result.get("Status") == "COMPLETED" and primary_eni:
        # Step 12 – attach original ENI to new instance
        created_arn = result.get("CreatedResourceArn", "")
        new_instance_id = created_arn.split("/")[-1] if "/" in created_arn else ""
        if new_instance_id:
            show_loading(stdscr, f"Attaching ENI {primary_eni} to {new_instance_id}...")
            try:
                attach_eni(primary_eni, new_instance_id, device_index=1)
                show_message(stdscr, "Success!", [
                    f"Restored instance:  {new_instance_id}",
                    f"Attached ENI:       {primary_eni}",
                    f"Original IP:        {target['PrivateIp']}",
                    "",
                    "The ENI has been attached as a secondary interface.",
                    "You may need to configure OS-level networking for the",
                    "secondary ENI to respond on the original IP.",
                ], C_SUCCESS)
            except Exception as e:
                show_message(stdscr, "ENI Attach Failed", [
                    f"Instance restored as {new_instance_id}",
                    f"But ENI attach failed: {e}",
                    f"Manually attach ENI {primary_eni} to the new instance.",
                ], C_ERROR)
        else:
            show_message(stdscr, "Restore Complete", [
                "Could not determine new instance ID from response.",
                f"Job ID: {job_id}",
                "Check AWS Console for the restored instance.",
            ], C_STATUS)


def workflow_new_instance_with_eni(stdscr):
    """Option 2: Restore to new instance, attach pre-created ENI."""
    # Step 1 – pick recovery point
    rp = select_vault_and_recovery_point(stdscr)
    if rp is None:
        return

    rp_arn = rp["RecoveryPointArn"]

    # Step 2 – pick available ENI
    show_loading(stdscr, "Loading available ENIs...")
    try:
        enis = list_enis()
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to list ENIs: {e}"], C_ERROR)
        return

    if not enis:
        show_message(stdscr, "No Available ENIs",
                     ["No ENIs with status 'available' found.",
                      "Create an ENI first, or use Option 1 instead."], C_ERROR)
        return

    def fmt_eni(eni):
        name = eni["Name"] or "(no name)"
        return f'{eni["NetworkInterfaceId"]}  │  {name}  │  {eni["PrivateIp"]}  │  {eni["SubnetId"][:20]}'

    idx = menu_select(stdscr, "Select ENI to attach to restored instance", enis, fmt_eni)
    if idx < 0:
        return
    chosen_eni = enis[idx]

    # Step 3 – get restore metadata
    show_loading(stdscr, "Fetching restore metadata...")
    try:
        vault_name = rp_arn.split(":")[-1].split("/")[0] if ":backup-vault:" not in rp_arn else \
            rp_arn.split(":backup-vault:")[-1].split("/")[0]
        meta_resp = aws.backup.get_recovery_point_restore_metadata(
            BackupVaultName=vault_name,
            RecoveryPointArn=rp_arn,
        )
        metadata = meta_resp.get("RestoreMetadata", {})
    except Exception as e:
        show_message(stdscr, "Error", [f"Failed to get metadata: {e}"], C_ERROR)
        return

    # Step 4 – IAM role
    default_role = get_iam_role_arn_for_restore()
    iam_role = input_text(stdscr, "IAM Role ARN for restore:", default_role)
    if not iam_role:
        return

    # Step 5 – optional instance type override
    current_type = metadata.get("InstanceType", "t3.medium")
    inst_type = input_text(stdscr, f"Instance type (current in backup: {current_type}):", current_type)

    # Step 6 – build metadata: restore in the same subnet as the ENI
    metadata["SubnetId"] = chosen_eni["SubnetId"]
    metadata["SecurityGroupIds"] = json.dumps(chosen_eni["SecurityGroups"])
    metadata["InstanceType"] = inst_type

    # Step 7 – confirm
    detail = [
        f"Recovery Point:  {rp_arn[-60:]}",
        f"Attach ENI:      {chosen_eni['NetworkInterfaceId']}",
        f"ENI Name:        {chosen_eni['Name'] or '(none)'}",
        f"ENI Private IP:  {chosen_eni['PrivateIp']}",
        f"Subnet:          {chosen_eni['SubnetId']}",
        f"Instance Type:   {inst_type}",
        "",
        "A new EC2 instance will be restored from backup.",
        "The selected ENI will be attached post-restore.",
    ]
    if not confirm_dialog(stdscr, "CONFIRM NEW INSTANCE RESTORE", detail):
        return

    # Step 8 – start restore
    show_loading(stdscr, "Starting restore job...")
    try:
        job_id = start_restore_job(rp_arn, metadata, iam_role)
    except Exception as e:
        show_message(stdscr, "Restore Failed", [f"Error: {e}"], C_ERROR)
        return

    # Step 9 – poll
    result = poll_restore_job(stdscr, job_id)

    if result and result.get("Status") == "COMPLETED":
        created_arn = result.get("CreatedResourceArn", "")
        new_instance_id = created_arn.split("/")[-1] if "/" in created_arn else ""
        if new_instance_id:
            show_loading(stdscr, f"Attaching ENI {chosen_eni['NetworkInterfaceId']}...")
            try:
                attach_eni(chosen_eni["NetworkInterfaceId"], new_instance_id, device_index=1)
                show_message(stdscr, "Success!", [
                    f"New instance:   {new_instance_id}",
                    f"Attached ENI:   {chosen_eni['NetworkInterfaceId']}",
                    f"ENI Private IP: {chosen_eni['PrivateIp']}",
                    "",
                    "ENI attached as secondary interface (device index 1).",
                    "Configure OS networking if needed.",
                ], C_SUCCESS)
            except Exception as e:
                show_message(stdscr, "ENI Attach Failed", [
                    f"Instance restored: {new_instance_id}",
                    f"ENI attach failed: {e}",
                    f"Manually attach: {chosen_eni['NetworkInterfaceId']}",
                ], C_ERROR)
        else:
            show_message(stdscr, "Restore Complete", [
                "Job completed but could not extract instance ID.",
                f"Job: {job_id}",
            ], C_STATUS)


# ──────────────────────────────────────────────────────────────────────
# Main Menu
# ──────────────────────────────────────────────────────────────────────

MAIN_MENU = [
    ("1", "Restore & Replace Instance", "Terminate existing EC2, restore from backup, reattach original ENI/IP"),
    ("2", "Restore New Instance + ENI", "Restore to new EC2 and attach a pre-created ENI"),
    ("q", "Quit", ""),
]


def draw_main_menu(stdscr, selected: int):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_title_bar(stdscr, "EC2 Backup Restore  ─  AWS CloudShell TUI")

    # ASCII art header
    logo = [
        "  ╔═══════════════════════════════════════════════════╗",
        "  ║       EC2  AWS  BACKUP  RESTORE  UTILITY         ║",
        "  ╚═══════════════════════════════════════════════════╝",
    ]
    for i, line in enumerate(logo):
        safe_addstr(stdscr, 2 + i, 2, line, curses.color_pair(C_ACCENT) | curses.A_BOLD)

    safe_addstr(stdscr, 6, 4, f"Region: {aws.region}", curses.color_pair(C_DIM))

    box_y, box_x, box_h, box_w = 8, 3, len(MAIN_MENU) * 3 + 3, min(70, w - 6)
    draw_box(stdscr, box_y, box_x, box_h, box_w, "Select Operation")

    for i, (key, label, desc) in enumerate(MAIN_MENU):
        row = box_y + 2 + i * 3
        is_sel = i == selected
        prefix = " ▸ " if is_sel else "   "
        attr = curses.color_pair(C_SELECT) | curses.A_BOLD if is_sel else curses.color_pair(C_MENU)
        safe_addstr(stdscr, row, box_x + 2, f"{prefix}[{key}]  {label}", attr)
        if desc:
            safe_addstr(stdscr, row + 1, box_x + 10, desc, curses.color_pair(C_DIM))

    draw_status_bar(stdscr, "↑/↓ Navigate  │  Enter Select  │  q Quit")
    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    init_colours()
    stdscr.timeout(-1)

    # Verify AWS credentials
    try:
        account = aws.get_account_id()
    except NoCredentialsError:
        show_message(stdscr, "No AWS Credentials",
                     ["Could not find AWS credentials.",
                      "Run this in AWS CloudShell or configure credentials."], C_ERROR)
        return
    except Exception as e:
        show_message(stdscr, "AWS Error", [f"Cannot connect to AWS: {e}"], C_ERROR)
        return

    selected = 0
    while True:
        draw_main_menu(stdscr, selected)
        key = stdscr.getch()

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
            elif choice == "q":
                break
        elif key == ord("1"):
            workflow_replace_instance(stdscr)
        elif key == ord("2"):
            workflow_new_instance_with_eni(stdscr)
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
