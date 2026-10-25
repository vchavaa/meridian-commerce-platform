#!/usr/bin/env python3
"""Offline evaluation of the cross-region backup-restore recovery sequence."""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class RecoveryResult:
    rpo_minutes: float
    rto_minutes: float
    steps: tuple


def simulate_recovery(
    incident_time,
    backup_time,
    restore_minutes,
    verification_minutes,
    cutover_minutes,
    rpo_target_minutes=60,
    rto_target_minutes=60,
):
    if backup_time > incident_time:
        raise ValueError("Recovery point must not be later than the incident")
    durations = (restore_minutes, verification_minutes, cutover_minutes)
    if any(duration < 0 for duration in durations):
        raise ValueError("Recovery phase durations must be non-negative")

    rpo_minutes = (incident_time - backup_time).total_seconds() / 60
    rto_minutes = sum(durations)
    if rpo_minutes > rpo_target_minutes:
        raise ValueError(f"RPO target exceeded: {rpo_minutes:.1f} > {rpo_target_minutes} minutes")
    if rto_minutes > rto_target_minutes:
        raise ValueError(f"RTO target exceeded: {rto_minutes:.1f} > {rto_target_minutes} minutes")

    return RecoveryResult(
        rpo_minutes=rpo_minutes,
        rto_minutes=rto_minutes,
        steps=(
            "Primary writes fenced",
            "Newest secondary-region recovery point selected",
            "Recovery instance restored",
            "Recovered database verified",
            "Application database target and traffic switched",
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-age-minutes", type=float, default=15)
    parser.add_argument("--restore-minutes", type=float, default=35)
    parser.add_argument("--verification-minutes", type=float, default=10)
    parser.add_argument("--cutover-minutes", type=float, default=5)
    parser.add_argument("--rpo-target-minutes", type=float, default=60)
    parser.add_argument("--rto-target-minutes", type=float, default=60)
    args = parser.parse_args()

    if args.backup_age_minutes < 0:
        parser.error("--backup-age-minutes must be non-negative")
    incident_time = datetime.now(timezone.utc)
    backup_time = incident_time - timedelta(minutes=args.backup_age_minutes)
    try:
        result = simulate_recovery(
            incident_time,
            backup_time,
            args.restore_minutes,
            args.verification_minutes,
            args.cutover_minutes,
            args.rpo_target_minutes,
            args.rto_target_minutes,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps({
        "mode": "offline_simulation_only",
        "live_cloud_actions": False,
        "rpo_minutes": result.rpo_minutes,
        "rto_minutes": result.rto_minutes,
        "steps": result.steps,
        "targets_met_in_simulation": True,
    }, indent=2))


if __name__ == "__main__":
    main()
