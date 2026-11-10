"""Unit tests for context-pressure decisions."""

from __future__ import annotations

import unittest

from relay.agent_session import ThreadState, TokenUsage
from relay.config import RelaySettings
from relay.context_monitor import (
    LEVEL_CHECKPOINT,
    LEVEL_HANDOFF,
    LEVEL_OK,
    LEVEL_PREPARE,
    LEVEL_URGENT,
    SOURCE_REAL,
    SOURCE_SIMULATED,
    SOURCE_UNAVAILABLE,
    ContextMonitor,
)


def state_with_usage(input_tokens: int, window: int | None) -> ThreadState:
    state = ThreadState(thread_id="t1")
    state.usage = TokenUsage(input_tokens=input_tokens, context_window=window)
    return state


class RealRatioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = ContextMonitor(RelaySettings())

    def test_real_ratio_is_computed_from_usage_and_window(self) -> None:
        sample = self.monitor.sample(state_with_usage(40_000, 100_000))
        self.assertEqual(sample.source, SOURCE_REAL)
        self.assertAlmostEqual(sample.ratio, 0.4)
        self.assertFalse(sample.simulated)

    def test_cached_prompt_tokens_are_not_double_counted(self) -> None:
        # inputTokens already contains the cached prefix; adding cached again
        # would report a window that is twice as full as it really is.
        usage = TokenUsage(
            input_tokens=40_000,
            cached_input_tokens=38_000,
            output_tokens=1_000,
            context_window=100_000,
        )
        self.assertEqual(usage.context_used_tokens(), 41_000)
        self.assertAlmostEqual(usage.context_ratio(), 0.41)

    def test_missing_window_yields_no_ratio(self) -> None:
        sample = self.monitor.sample(state_with_usage(40_000, None))
        self.assertIsNone(sample.ratio)
        self.assertEqual(sample.source, SOURCE_UNAVAILABLE)

    def test_no_usage_yields_no_ratio(self) -> None:
        sample = self.monitor.sample(ThreadState(thread_id="t1"))
        self.assertIsNone(sample.ratio)

    def test_decision_levels_follow_thresholds(self) -> None:
        cases = {
            0.10: LEVEL_OK,
            0.66: LEVEL_CHECKPOINT,
            0.76: LEVEL_PREPARE,
            0.81: LEVEL_HANDOFF,
            0.95: LEVEL_URGENT,
        }
        for ratio, expected in cases.items():
            sample = self.monitor.sample(state_with_usage(int(ratio * 100_000), 100_000))
            decision = self.monitor.decide(sample)
            self.assertEqual(decision.level, expected, f"ratio={ratio}")


class SimulatedRatioTests(unittest.TestCase):
    def test_simulated_ratio_is_labelled(self) -> None:
        monitor = ContextMonitor(RelaySettings(), simulated_ratio=0.8)
        sample = monitor.sample(ThreadState(thread_id="t1"))
        self.assertEqual(sample.source, SOURCE_SIMULATED)
        self.assertTrue(sample.simulated)
        self.assertAlmostEqual(sample.ratio, 0.8)
        self.assertTrue(monitor.decide(sample).needs_handoff)

    def test_simulated_ratio_overrides_measured_usage(self) -> None:
        monitor = ContextMonitor(RelaySettings(), simulated_ratio=0.8)
        sample = monitor.sample(state_with_usage(1_000, 500_000))
        self.assertEqual(sample.source, SOURCE_SIMULATED)


class FallbackSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = ContextMonitor(RelaySettings(), fallback_turn_limit=5)

    def test_compaction_triggers_handoff_without_any_ratio(self) -> None:
        state = ThreadState(thread_id="t1")
        state.compaction_events = 1
        sample = self.monitor.sample(state)
        self.assertIsNone(sample.ratio)
        decision = self.monitor.decide(sample)
        self.assertEqual(decision.level, LEVEL_HANDOFF)
        self.assertTrue(decision.needs_handoff)

    def test_turn_count_fallback_prepares_but_does_not_hand_off(self) -> None:
        sample = self.monitor.sample(ThreadState(thread_id="t1"))
        decision = self.monitor.decide(sample, turns_since_checkpoint=5)
        self.assertEqual(decision.level, LEVEL_PREPARE)
        self.assertFalse(decision.needs_handoff)

    def test_no_signal_stays_ok_and_never_invents_a_percentage(self) -> None:
        sample = self.monitor.sample(ThreadState(thread_id="t1"))
        decision = self.monitor.decide(sample, turns_since_checkpoint=0)
        self.assertEqual(decision.level, LEVEL_OK)
        self.assertIsNone(decision.sample.ratio)
        self.assertIn("unmeasured", decision.sample.describe())


if __name__ == "__main__":
    unittest.main()
