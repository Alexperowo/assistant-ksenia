import unittest

from butler.resilience import RepeatingFailurePolicy


class RepeatingFailurePolicyTests(unittest.TestCase):
    def test_repeated_failure_uses_bounded_backoff_and_silent_retries(self):
        policy = RepeatingFailurePolicy(
            base_delay_seconds=5,
            max_delay_seconds=20,
            reminder_seconds=300,
        )

        decisions = [policy.record_failure(float(second)) for second in (0, 5, 15, 35)]

        self.assertEqual([item.delay_seconds for item in decisions], [5, 10, 20, 20])
        self.assertEqual([item.announce for item in decisions], [True, False, False, False])

    def test_reminder_and_recovery_reset_are_explicit(self):
        policy = RepeatingFailurePolicy(reminder_seconds=300)
        self.assertTrue(policy.record_failure(10).announce)
        self.assertTrue(policy.record_failure(311).announce)

        policy.reset()

        recovered = policy.record_failure(312)
        self.assertEqual(recovered.failure_count, 1)
        self.assertTrue(recovered.announce)


if __name__ == "__main__":
    unittest.main()
