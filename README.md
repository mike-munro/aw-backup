# aw-backup

A terminal user interface (TUI) for restoring EC2 instances from AWS Backup recovery points, designed to run in AWS CloudShell.

## Features

- Browse backup vaults and recovery points grouped by instance
- Two restore modes (see below)
- Interactive tag configuration before each restore
- Full-screen restore review before submitting the job
- Live restore job progress monitor

## Requirements

- AWS CloudShell (boto3 and curses are pre-installed), **or** any environment with `boto3` and Python 3.8+
- IAM permissions: `backup:*`, `ec2:Describe*`, `ec2:TerminateInstances`, `ec2:AttachNetworkInterface`, `iam:GetRole`, `sts:GetCallerIdentity`

## Usage

```bash
python3 aws-backup-restore-tui.py
```

## Restore Modes

### 1 — Replace Existing Instance

Terminates a running/stopped EC2 instance and restores it from a backup recovery point. The primary ENI of the terminated instance is re-attached to the new instance so it retains the same private IP address.

**Steps:**
1. Select backup vault → instance → recovery point
2. Select the target instance to terminate
3. Enter IAM role ARN (defaults to `AWSBackupCustomServiceRole`)
4. Configure tags (see [Tag configuration](#tag-configuration))
5. Review full restore plan → confirm

### 2 — Restore New Instance + ENI

Restores to a brand-new EC2 instance and attaches a pre-created available ENI as the primary interface (device index 0). The ENI must already exist with status `available`.

**Steps:**
1. Select backup vault → instance → recovery point
2. Select an available ENI to attach
3. Enter IAM role ARN
4. Confirm or change instance type
5. Configure tags (see [Tag configuration](#tag-configuration))
6. Review full restore plan → confirm

## Tag Configuration

During both restore workflows, you are prompted to configure tags before the restore job is submitted:

| Prompt | Default | Description |
|--------|---------|-------------|
| **Keep existing tags?** | `y` (yes) | Preserve all tags from the source instance. Type `n` to discard them. |
| **Name tag for restored instance** | `restore-<original name>` | The `Name` tag applied to the restored instance. Edit freely or press Enter to accept the default. |

If the source instance had no `Name` tag, the default is `restore-`.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `PgUp` / `PgDn` | Scroll one page |
| `g` / `Home` | Jump to first item |
| `G` / `End` | Jump to last item |
| `/` | Search / filter current list |
| `Enter` | Select item |
| `q` / `Esc` | Go back / cancel |
| `?` | Show help |
| `y` | Confirm (restore review screen) |
| `n` | Cancel (restore review screen) |

## Default IAM Role

The tool looks up `AWSBackupCustomServiceRole` via IAM and pre-fills the role ARN prompt. You can override this at the prompt if needed.

## Notes

- Termination in mode 1 is irreversible — review the restore plan carefully before confirming.
- ENI re-attachment in mode 1 uses device index 1 (secondary interface). OS-level routing may be required.
- ENI attachment in mode 2 uses device index 0 (primary interface).
- If `GetRecoveryPointRestoreMetadata` is denied, the tool falls back to building metadata from the current instance/ENI configuration.
- Restore jobs that are started but not monitored to completion continue running in the background. Check their status with:

```bash
aws backup describe-restore-job --restore-job-id <id>
```
