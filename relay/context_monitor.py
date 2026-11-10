"""Context-pressure monitoring.

Two data sources feed this module:

* **real** — ``thread/tokenUsage/updated`` notifications carry the input token
  count of the last model response and the model context window. A ratio is
  only computed when both numbers are present; the Relay never invents a
  percentage.
* **simulated** — Level 2/3 tests inject a ratio directly, flagged as simulated
  so it can never be mistaken for measured usage.

When no reliable ratio is available the monitor falls back to signals that are
verifiable: the number of turns since the last checkpoint and Codex's own
compaction events. Compaction is treated as proof that the context was
genuinely full, which justifies a handoff without any invented number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_session import ThreadState
from .config import RelaySettings

LEVEL_OK = "ok"
LEVEL_CHECKPOINT = "checkpoint"
LEVEL_PREPARE = "prepare"
LEVEL_HANDOFF = "handoff"
LEVEL_URGENT = "urgent"

LEVEL_ORDER = {
    LEVEL_OK: 0,
    LEVEL_CHECKPOINT: 1,
    LEVEL_PREPARE: 2,
    LEVEL_HANDOFF: 3,
    LEVEL_URGENT: 4,
}

SOURCE_REAL = "real"
SOURCE_SIMULATED = "simulated"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class PressureSample:
    """One observation of context pressure."""

    ratio: float | None
    source: str
    context_used_tokens: int | None
    context_window: int | None
    turn_count: int
    compaction_events: int
    simulated: bool

    def describe(self) -> str:
        """Return a short human-readable description for logs and reports."""
        if self.ratio is None:
            return (
                f"unmeasured (turns={self.turn_count}, "
                f"compactions={self.compaction_events}, source={self.source})"
            )
        marker = " [SIMULATED]" if self.simulated else ""
        used = f"{self.context_used_tokens}/{self.context_window} tokens"
        return f"{self.ratio:.1%} ({used}, {self.source}){marker}"


@dataclass(frozen=True)
class PressureDecision:
    """What the controller should do at the next safe boundary."""

    level: str
    reason: str
    sample: PressureSample

    @property
    def needs_handoff(self) -> bool:
        """Return True when this decision requires a session handoff."""
        return self.level in (LEVEL_HANDOFF, LEVEL_URGENT)

    @property
    def at_least_prepare(self) -> bool:
        """Return True when at least a handoff package should be prepared."""
        return LEVEL_ORDER[self.level] >= LEVEL_ORDER[LEVEL_PREPARE]

    @property
    def at_least_checkpoint(self) -> bool:
        """Return True when at least a checkpoint should be written."""
        return LEVEL_ORDER[self.level] >= LEVEL_ORDER[LEVEL_CHECKPOINT]


class ContextMonitor:
    """Turns thread observations into checkpoint/handoff decisions."""

    def __init__(
        self,
        settings: RelaySettings,
        *,
        simulated_ratio: float | None = None,
        fallback_turn_limit: int = 40,
    ) -> None:
        if simulated_ratio is not None and not 0.0 < simulated_ratio <= 1.0:
            raise ValueError("simulated_ratio must be within (0, 1]")
        self.settings = settings
        self.simulated_ratio = simulated_ratio
        self.fallback_turn_limit = max(1, fallback_turn_limit)

    @property
    def simulating(self) -> bool:
        """Return True when a simulated ratio is injected."""
        return self.simulated_ratio is not None

    def sample(self, thread_state: ThreadState | None) -> PressureSample:
        """Collect one pressure sample from a thread observation (or nothing)."""
        turn_count = thread_state.turn_count if thread_state else 0
        compaction_events = thread_state.compaction_events if thread_state else 0

        if self.simulated_ratio is not None:
            return PressureSample(
                ratio=self.simulated_ratio,
                source=SOURCE_SIMULATED,
                context_used_tokens=None,
                context_window=None,
                turn_count=turn_count,
                compaction_events=compaction_events,
                simulated=True,
            )

        usage = thread_state.usage if thread_state else None
        if usage is None:
            return PressureSample(
                ratio=None,
                source=SOURCE_UNAVAILABLE,
                context_used_tokens=None,
                context_window=None,
                turn_count=turn_count,
                compaction_events=compaction_events,
                simulated=False,
            )

        ratio = usage.context_ratio()
        if ratio is None:
            return PressureSample(
                ratio=None,
                source=SOURCE_UNAVAILABLE,
                context_used_tokens=usage.context_used_tokens(),
                context_window=usage.context_window,
                turn_count=turn_count,
                compaction_events=compaction_events,
                simulated=False,
            )
        return PressureSample(
            ratio=ratio,
            source=SOURCE_REAL,
            context_used_tokens=usage.context_used_tokens(),
            context_window=usage.context_window,
            turn_count=turn_count,
            compaction_events=compaction_events,
            simulated=False,
        )

    def decide(self, sample: PressureSample, *, turns_since_checkpoint: int = 0) -> PressureDecision:
        """Map a sample onto the configured thresholds or verifiable fallbacks."""
        if sample.ratio is not None:
            if sample.ratio >= self.settings.urgent_threshold:
                return PressureDecision(
                    LEVEL_URGENT,
                    f"context at {sample.ratio:.1%} >= urgent threshold "
                    f"{self.settings.urgent_threshold:.0%}",
                    sample,
                )
            if sample.ratio >= self.settings.context_threshold:
                return PressureDecision(
                    LEVEL_HANDOFF,
                    f"context at {sample.ratio:.1%} >= handoff threshold "
                    f"{self.settings.context_threshold:.0%}",
                    sample,
                )
            if sample.ratio >= self.settings.prepare_threshold:
                return PressureDecision(
                    LEVEL_PREPARE,
                    f"context at {sample.ratio:.1%} >= prepare threshold "
                    f"{self.settings.prepare_threshold:.0%}",
                    sample,
                )
            if sample.ratio >= self.settings.checkpoint_threshold:
                return PressureDecision(
                    LEVEL_CHECKPOINT,
                    f"context at {sample.ratio:.1%} >= checkpoint threshold "
                    f"{self.settings.checkpoint_threshold:.0%}",
                    sample,
                )
            return PressureDecision(
                LEVEL_OK, f"context at {sample.ratio:.1%} below checkpoint threshold", sample
            )

        # No trustworthy ratio: use only verifiable, non-numeric signals.
        if sample.compaction_events > 0:
            return PressureDecision(
                LEVEL_HANDOFF,
                f"context compaction observed ({sample.compaction_events}x); "
                "treating the window as full without inventing a percentage",
                sample,
            )
        if turns_since_checkpoint >= self.fallback_turn_limit:
            return PressureDecision(
                LEVEL_PREPARE,
                f"{turns_since_checkpoint} turns since the last checkpoint "
                f"(fallback limit {self.fallback_turn_limit})",
                sample,
            )
        return PressureDecision(
            LEVEL_OK,
            "no reliable context usage reported; relying on turn counting",
            sample,
        )

    def describe_settings(self) -> dict[str, Any]:
        """Return the effective thresholds for reports and logs."""
        return {
            "checkpoint_threshold": self.settings.checkpoint_threshold,
            "prepare_threshold": self.settings.prepare_threshold,
            "context_threshold": self.settings.context_threshold,
            "urgent_threshold": self.settings.urgent_threshold,
            "auto_handoff": self.settings.auto_handoff,
            "simulated_ratio": self.simulated_ratio,
            "fallback_turn_limit": self.fallback_turn_limit,
        }
