import unittest
from datetime import datetime, timedelta, timezone

from scripts.simulate_disaster_recovery import simulate_recovery


class DisasterRecoverySimulationTests(unittest.TestCase):
    def setUp(self):
        self.incident_time = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

    def test_recovery_sequence_meets_default_targets(self):
        result = simulate_recovery(
            self.incident_time,
            self.incident_time - timedelta(minutes=15),
            restore_minutes=35,
            verification_minutes=10,
            cutover_minutes=5,
        )

        self.assertEqual(result.rpo_minutes, 15)
        self.assertEqual(result.rto_minutes, 50)
        self.assertEqual(result.steps[0], "Primary writes fenced")
        self.assertTrue(result.steps[-1].startswith("Application database target"))

    def test_rpo_target_violation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "RPO target exceeded"):
            simulate_recovery(
                self.incident_time,
                self.incident_time - timedelta(minutes=61),
                restore_minutes=10,
                verification_minutes=10,
                cutover_minutes=5,
            )

    def test_rto_target_violation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "RTO target exceeded"):
            simulate_recovery(
                self.incident_time,
                self.incident_time - timedelta(minutes=5),
                restore_minutes=40,
                verification_minutes=15,
                cutover_minutes=10,
            )

    def test_recovery_point_after_incident_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "later than the incident"):
            simulate_recovery(
                self.incident_time,
                self.incident_time + timedelta(minutes=1),
                restore_minutes=10,
                verification_minutes=10,
                cutover_minutes=5,
            )


if __name__ == "__main__":
    unittest.main()
