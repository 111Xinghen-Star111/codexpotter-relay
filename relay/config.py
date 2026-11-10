"""Configuration loading and validation for CodexPotter Relay.

The Relay reads one configuration file (``<relay home>/relay.toml`` by default).
Both TOML and the small YAML subset shown in the project requirements are
accepted, because operators copy the documented YAML block into their config.

Every setting is validated on load: thresholds must be ordered, budgets must be
positive, and unknown keys are rejected so a typo can never silently disable a
safety limit.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import config_path


class ConfigError(ValueError):
    """Raised when a configuration file is unreadable or invalid."""


@dataclass(frozen=True)
class RelaySettings:
    """Context-pressure thresholds and core loop limits."""

    checkpoint_threshold: float = 0.65
    prepare_threshold: float = 0.75
    context_threshold: float = 0.80
    urgent_threshold: float = 0.90
    auto_handoff: bool = True
    max_handoffs: int = 32
    max_turns: int = 200
    max_model_requests: int = 400
    token_budget: int | None = None
    model: str | None = None
    effort: str | None = None
    max_handoff_retries: int = 3
    turn_timeout_seconds: float = 7200.0
    #: Turns since the last checkpoint that trigger a handoff preparation when
    #: Codex reports no usable token usage at all.
    fallback_turn_limit: int = 40

    def validate(self) -> None:
        ordered = (
            self.checkpoint_threshold,
            self.prepare_threshold,
            self.context_threshold,
            self.urgent_threshold,
        )
        for value in ordered:
            if not 0.0 < value <= 1.0:
                raise ConfigError(
                    f"relay thresholds must be within (0, 1], got {value!r}"
                )
        if list(ordered) != sorted(ordered) or len(set(ordered)) != len(ordered):
            raise ConfigError(
                "relay thresholds must increase: "
                "checkpoint < prepare < context < urgent, got "
                f"{ordered}"
            )
        for name in ("max_handoffs", "max_turns", "max_model_requests"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"relay.{name} must be positive")
        if self.token_budget is not None and self.token_budget <= 0:
            raise ConfigError("relay.token_budget must be positive when set")
        if self.max_handoff_retries < 0:
            raise ConfigError("relay.max_handoff_retries must be >= 0")
        if self.turn_timeout_seconds <= 0:
            raise ConfigError("relay.turn_timeout_seconds must be positive")
        if self.fallback_turn_limit <= 0:
            raise ConfigError("relay.fallback_turn_limit must be positive")


@dataclass(frozen=True)
class TestingSettings:
    """Simulated-pressure test settings (Level 1/2/3)."""

    enabled: bool = False
    simulate_context_pressure: bool = False
    simulated_usage_ratio: float = 0.80
    max_handoffs: int = 3
    max_model_requests: int = 16
    max_turns: int = 12
    real_pressure_enabled: bool = False
    context_window_override: int | None = None
    auto_compact_token_limit: int | None = None

    def validate(self) -> None:
        if not 0.0 < self.simulated_usage_ratio <= 1.0:
            raise ConfigError("testing.simulated_usage_ratio must be within (0, 1]")
        if self.enabled and not self.simulate_context_pressure and not self.real_pressure_enabled:
            raise ConfigError(
                "testing.enabled requires either simulate_context_pressure or "
                "real_pressure_enabled; refusing to run a test that can never trigger"
            )
        for name in ("max_handoffs", "max_model_requests", "max_turns"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"testing.{name} must be positive")
        if self.context_window_override is not None and self.context_window_override < 1000:
            raise ConfigError("testing.context_window_override must be >= 1000 tokens")
        if self.auto_compact_token_limit is not None and self.auto_compact_token_limit < 1000:
            raise ConfigError("testing.auto_compact_token_limit must be >= 1000 tokens")


@dataclass(frozen=True)
class ControllerSettings:
    """Process-level settings for the app-server connection and polling."""

    codex_executable: str | None = None
    sandbox: str = "workspace-write"
    approvals_reviewer: str = "auto_review"
    approval_policy: str = "on-request"
    poll_interval_seconds: float = 1.0
    startup_timeout_seconds: float = 120.0
    keep_app_server_stdout: bool = False

    def validate(self) -> None:
        if self.sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise ConfigError(f"controller.sandbox is not a valid mode: {self.sandbox!r}")
        if self.approvals_reviewer not in ("user", "auto_review", "guardian_subagent"):
            raise ConfigError(
                "controller.approvals_reviewer must be user, auto_review or guardian_subagent"
            )
        if self.approval_policy not in ("untrusted", "on-request", "never"):
            raise ConfigError(
                "controller.approval_policy must be untrusted, on-request or never"
            )
        if self.poll_interval_seconds <= 0:
            raise ConfigError("controller.poll_interval_seconds must be positive")


@dataclass(frozen=True)
class RelayConfig:
    """Full configuration, composed of the three documented sections."""

    relay: RelaySettings = field(default_factory=RelaySettings)
    testing: TestingSettings = field(default_factory=TestingSettings)
    controller: ControllerSettings = field(default_factory=ControllerSettings)
    source_path: Path | None = None

    def validate(self) -> None:
        self.relay.validate()
        self.testing.validate()
        self.controller.validate()

    def with_testing_overrides(self, **overrides: Any) -> "RelayConfig":
        """Return a copy with ``testing`` fields replaced and re-validated."""
        from dataclasses import replace

        merged = replace(self.testing, **overrides)
        config = replace(self, testing=merged)
        config.validate()
        return config


_SECTION_FACTORIES = {
    "relay": RelaySettings,
    "testing": TestingSettings,
    "controller": ControllerSettings,
}


def _build_section(name: str, payload: Any) -> Any:
    factory = _SECTION_FACTORIES[name]
    if payload is None:
        return factory()
    if not isinstance(payload, dict):
        raise ConfigError(f"section [{name}] must be a mapping, got {type(payload).__name__}")
    allowed = {field_name for field_name in factory.__annotations__}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigError(
            f"section [{name}] has unknown keys: {', '.join(unknown)}; "
            f"allowed keys are: {', '.join(sorted(allowed))}"
        )
    return factory(**payload)


def parse_config_text(text: str, source: str = "<string>") -> RelayConfig:
    """Parse configuration text written either as TOML or as the documented YAML."""
    if not text.strip():
        config = RelayConfig()
        config.validate()
        return config

    stripped = text.lstrip()
    if stripped.startswith("{"):
        raise ConfigError(f"{source}: a JSON object is not a supported config format")

    try:
        if _looks_like_toml(text):
            raw = tomllib.loads(text)
        else:
            raw = _parse_yaml_subset(text, source)
    except (tomllib.TOMLDecodeError, ConfigError) as error:
        raise ConfigError(f"{source}: {error}") from error

    unknown_sections = sorted(set(raw) - set(_SECTION_FACTORIES))
    if unknown_sections:
        raise ConfigError(
            f"{source}: unknown sections: {', '.join(unknown_sections)}; "
            f"allowed sections are: {', '.join(sorted(_SECTION_FACTORIES))}"
        )
    config = RelayConfig(
        relay=_build_section("relay", raw.get("relay")),
        testing=_build_section("testing", raw.get("testing")),
        controller=_build_section("controller", raw.get("controller")),
    )
    config.validate()
    return config


def load_config(path: Path | None = None) -> RelayConfig:
    """Load configuration from ``path`` (default: the standard config path)."""
    target = path or config_path()
    if not target.exists():
        config = RelayConfig()
        config.validate()
        return config
    text = target.read_text(encoding="utf-8")
    config = parse_config_text(text, source=str(target))
    from dataclasses import replace

    return replace(config, source_path=target)


def _looks_like_toml(text: str) -> bool:
    """Return True when the text uses TOML section headers or ``=`` assignments."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            return True
        if "=" in line and ":" not in line.split("=", 1)[0]:
            return True
        if ":" in line:
            return False
    return False


def _parse_yaml_subset(text: str, source: str) -> dict[str, Any]:
    """Parse the nested-mapping YAML subset used by the requirements document.

    Supported: two-level mappings of ``key: value`` pairs, ``#`` comments,
    quoted strings, booleans, integers and floats. Lists and anchors are
    rejected with an explicit message instead of being silently ignored.
    """
    result: dict[str, Any] = {}
    current_section: str | None = None
    section_indent: int | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        without_comment = _strip_yaml_comment(raw_line)
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        content = without_comment.strip()
        if content.startswith("- "):
            raise ConfigError(
                f"{source}:{line_number}: YAML lists are not supported; "
                "use TOML for nested structures"
            )
        if ":" not in content:
            raise ConfigError(
                f"{source}:{line_number}: expected 'key: value', got {content!r}"
            )
        key, _, value_text = content.partition(":")
        key = key.strip()
        if not key:
            raise ConfigError(f"{source}:{line_number}: empty key")
        value_text = value_text.strip()

        if not value_text:
            if indent != 0:
                raise ConfigError(
                    f"{source}:{line_number}: nested section {key!r} must not be indented"
                )
            if key in result:
                raise ConfigError(f"{source}:{line_number}: duplicate section {key!r}")
            result[key] = {}
            current_section = key
            section_indent = None
            continue

        if indent == 0:
            raise ConfigError(
                f"{source}:{line_number}: top-level scalar {key!r} is not supported; "
                "configuration is grouped into sections"
            )
        if current_section is None:
            raise ConfigError(
                f"{source}:{line_number}: {key!r} appears before any section header"
            )
        if section_indent is None:
            section_indent = indent
        if indent != section_indent:
            raise ConfigError(
                f"{source}:{line_number}: unexpected indentation {indent}, "
                f"expected {section_indent}"
            )
        section = result[current_section]
        if key in section:
            raise ConfigError(f"{source}:{line_number}: duplicate key {key!r}")
        section[key] = _parse_yaml_scalar(value_text, source, line_number)

    return result


def _strip_yaml_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    quote: str | None = None
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
            continue
        if character == "#":
            return line[:index]
    return line


def _parse_yaml_scalar(text: str, source: str, line_number: int) -> Any:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", "~"):
        return None
    try:
        if "." in text or "e" in lowered:
            return float(text)
        return int(text)
    except ValueError as error:
        raise ConfigError(
            f"{source}:{line_number}: cannot parse value {text!r} as a number or boolean"
        ) from error


DEFAULT_CONFIG_TEMPLATE = """\
# CodexPotter Relay configuration.
# Thresholds are ratios of the model context window (0-1).

[relay]
checkpoint_threshold = 0.65   # save a checkpoint
prepare_threshold = 0.75      # build the handoff package
context_threshold = 0.80      # hand off at the next safe boundary
urgent_threshold = 0.90       # hand off immediately at the next boundary
auto_handoff = true
max_handoffs = 32
max_turns = 200
max_model_requests = 400
# token_budget = 2000000     # optional hard token ceiling per chain
# model = "gpt-6.1-sol"      # default: inherit the Codex configuration
# effort = "high"            # default: inherit the Codex configuration
max_handoff_retries = 3
turn_timeout_seconds = 7200
fallback_turn_limit = 40

[testing]
# Level 2/3 tests. Simulated pressure never writes into production chains.
enabled = false
simulate_context_pressure = false
simulated_usage_ratio = 0.80
max_handoffs = 3
max_model_requests = 16
max_turns = 12
# real_pressure_enabled = false
# context_window_override = 24000   # official Codex config: model_context_window
# auto_compact_token_limit = 16000  # official Codex config: model_auto_compact_token_limit

[controller]
# codex_executable = "C:/path/to/codex.exe"
sandbox = "workspace-write"
approvals_reviewer = "auto_review"
approval_policy = "on-request"
poll_interval_seconds = 1.0
startup_timeout_seconds = 120
keep_app_server_stdout = false
"""


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write the documented default configuration file and return its path."""
    target = path or config_path()
    if target.exists() and not overwrite:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return target
