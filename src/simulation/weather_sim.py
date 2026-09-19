"""
Weather simulation layer.

Generates a sequence of progressively worsening (and recovering) weather
observations driven either by live Open-Meteo data for a selected location,
or by a deterministic scripted sequence if the network is unavailable.

The simulation is *controlled*: it advances through named phases that are
consumed by the demo to show the full narrative arc:

    calm → advisory → storm_approaching → peak_storm → degradation_peak
    → maintenance_triggered → recovery

Each phase maps to a multiplier applied to the base weather values of the
selected demo asset (TX-007 by default — highest existing weather stress).
The static demo data is always the fallback; the simulation only *adds* a
layer on top.

DESIGN CONTRACT
---------------
- The simulator is pure / stateless per ``advance()``.  Callers own the
  state machine (which phase they are in).
- Live weather from Open-Meteo is used *only* as the base for the multiplier
  scaling; it never overrides the scoring weights or thresholds.
- If the live fetch fails, the static demo weather for TX-007 is used as the
  base — the demo remains fully functional offline.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from data.raw_types import RawWeatherObservation
from weather.open_meteo import fetch_weather_with_fallback

# Static base weather for TX-007 (London Waterfront area) — always available
# as the offline fallback regardless of network state.
_TX007_STATIC = RawWeatherObservation(
    max_temp_c=38.0,
    min_temp_c=26.0,
    precipitation_mm=82.0,
    wind_speed_max_kmh=115.0,
    storm_warning_level=3,
    forecast_hours=72,
)


class SimulationPhase(str, Enum):
    """Named phases of the live simulation narrative."""
    CALM = "calm"
    ADVISORY = "advisory"
    STORM_APPROACHING = "storm_approaching"
    PEAK_STORM = "peak_storm"
    DEGRADATION_PEAK = "degradation_peak"
    MAINTENANCE_TRIGGERED = "maintenance_triggered"
    RECOVERY = "recovery"


# ---------------------------------------------------------------------------
# Phase metadata — describes each phase for the UI narrative banner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhaseInfo:
    phase: SimulationPhase
    label: str
    description: str
    # Multipliers applied to the *base* weather values (1.0 = same as base)
    temp_mult: float = 1.0
    precip_mult: float = 1.0
    wind_mult: float = 1.0
    # Override storm warning level; None means derive from scaled values
    storm_level_override: Optional[int] = None
    # Sensor degradation pressure added to assets per phase (0.0–1.0 scale)
    # Used by asset_sim to amplify sensor readings proportionally.
    sensor_pressure: float = 0.0


# Scripted narrative phases for the live simulation demo
DEMO_PHASES: list[PhaseInfo] = [
    PhaseInfo(
        phase=SimulationPhase.CALM,
        label="Normal operations",
        description="Fleet operating within normal parameters. No weather events forecast.",
        temp_mult=0.55, precip_mult=0.15, wind_mult=0.20,
        storm_level_override=0,
        sensor_pressure=0.0,
    ),
    PhaseInfo(
        phase=SimulationPhase.ADVISORY,
        label="Weather advisory issued",
        description="Moderate heat and wind advisory in effect. Monitor high-risk assets closely.",
        temp_mult=0.70, precip_mult=0.35, wind_mult=0.45,
        storm_level_override=1,
        sensor_pressure=0.10,
    ),
    PhaseInfo(
        phase=SimulationPhase.STORM_APPROACHING,
        label="Storm approaching",
        description="Storm system intensifying. Elevated sensor stress on ageing assets.",
        temp_mult=0.85, precip_mult=0.65, wind_mult=0.70,
        storm_level_override=2,
        sensor_pressure=0.25,
    ),
    PhaseInfo(
        phase=SimulationPhase.PEAK_STORM,
        label="Peak storm — asset degradation active",
        description="Severe storm at peak. Critical assets showing rapid sensor deterioration.",
        temp_mult=1.00, precip_mult=1.00, wind_mult=1.00,
        storm_level_override=3,
        sensor_pressure=0.55,
    ),
    PhaseInfo(
        phase=SimulationPhase.DEGRADATION_PEAK,
        label="Peak degradation — risk threshold breached",
        description="Multiple assets crossed risk thresholds. Maintenance scheduler triggered.",
        temp_mult=1.00, precip_mult=1.00, wind_mult=1.00,
        storm_level_override=3,
        sensor_pressure=0.75,
    ),
    PhaseInfo(
        phase=SimulationPhase.MAINTENANCE_TRIGGERED,
        label="Maintenance triggered — crews dispatched",
        description="Deterministic scheduler has reordered the maintenance plan. Crews deploying.",
        temp_mult=0.80, precip_mult=0.70, wind_mult=0.65,
        storm_level_override=2,
        sensor_pressure=0.50,
    ),
    PhaseInfo(
        phase=SimulationPhase.RECOVERY,
        label="Recovery — risk normalising",
        description="Storm passing. Preventive maintenance has reduced risk levels.",
        temp_mult=0.60, precip_mult=0.25, wind_mult=0.30,
        storm_level_override=1,
        sensor_pressure=0.15,
    ),
]

# Fast lookup by phase enum value
_PHASE_MAP: dict[SimulationPhase, PhaseInfo] = {p.phase: p for p in DEMO_PHASES}


class WeatherSimulator:
    """
    Controls the simulation phase and derives weather observations.

    The simulator holds the *current* phase index and the base weather from
    which all phase-scaled observations are derived.  It is safe to advance
    from any phase to any other (not restricted to sequential progression)
    so the UI can jump to any point in the narrative.

    Parameters
    ----------
    base_lat, base_lon:
        Geographic coordinates used for the live Open-Meteo lookup.
        Defaults to the TX-007 Waterfront Plaza location (London).
    attempt_live:
        If True, attempt a live Open-Meteo fetch on construction.  On
        network failure the static TX-007 base weather is used instead.
    current_phase:
        Starting phase for the simulation (default: CALM).
    """

    # TX-007 Waterfront Plaza default location
    DEFAULT_LAT = 51.507
    DEFAULT_LON = -0.060

    def __init__(
        self,
        base_lat: float = DEFAULT_LAT,
        base_lon: float = DEFAULT_LON,
        *,
        attempt_live: bool = True,
        current_phase: SimulationPhase = SimulationPhase.CALM,
    ) -> None:
        self.lat = base_lat
        self.lon = base_lon
        self.current_phase = current_phase
        self.live_base = False

        if attempt_live:
            obs, self.live_base = fetch_weather_with_fallback(
                lat=base_lat,
                lon=base_lon,
                fallback=_TX007_STATIC,
                forecast_hours=72,
                timeout_s=8.0,
            )
            self._base = obs
        else:
            self._base = copy.copy(_TX007_STATIC)

    @property
    def phase_info(self) -> PhaseInfo:
        """Metadata for the current phase."""
        return _PHASE_MAP[self.current_phase]

    @property
    def phase_index(self) -> int:
        """0-based index of the current phase in ``DEMO_PHASES``."""
        for i, p in enumerate(DEMO_PHASES):
            if p.phase == self.current_phase:
                return i
        return 0

    def advance(self) -> SimulationPhase:
        """Advance to the next phase (wraps at end).  Returns the new phase."""
        idx = self.phase_index
        next_idx = (idx + 1) % len(DEMO_PHASES)
        self.current_phase = DEMO_PHASES[next_idx].phase
        return self.current_phase

    def set_phase(self, phase: SimulationPhase) -> None:
        """Jump directly to any named phase."""
        self.current_phase = phase

    def current_weather(self) -> RawWeatherObservation:
        """Return the base weather scaled by the current phase multipliers."""
        return self._scale(self._base, _PHASE_MAP[self.current_phase])

    @staticmethod
    def _scale(
        base: RawWeatherObservation,
        info: PhaseInfo,
    ) -> RawWeatherObservation:
        """Apply phase multipliers to a base weather observation."""
        max_temp = round(base.max_temp_c * info.temp_mult, 1)
        min_temp = round(base.min_temp_c * info.temp_mult, 1)
        precip = round(base.precipitation_mm * info.precip_mult, 1)
        wind = round(base.wind_speed_max_kmh * info.wind_mult, 1)

        level: int
        if info.storm_level_override is not None:
            level = info.storm_level_override
        else:
            from weather.storm_classifier import classify_storm_level, DEFAULT_CLASSIFIER_CONFIG
            level = classify_storm_level(
                max_temp_c=max_temp,
                min_temp_c=min_temp,
                precipitation_mm=precip,
                wind_speed_max_kmh=wind,
                config=DEFAULT_CLASSIFIER_CONFIG,
            )

        return RawWeatherObservation(
            max_temp_c=max_temp,
            min_temp_c=min_temp,
            precipitation_mm=precip,
            wind_speed_max_kmh=wind,
            storm_warning_level=level,
            forecast_hours=base.forecast_hours,
        )
