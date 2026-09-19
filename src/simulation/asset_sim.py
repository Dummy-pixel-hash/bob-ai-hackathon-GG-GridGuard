"""
Asset condition simulation.

Takes the current simulation phase's ``sensor_pressure`` and applies it to a
``RawAssetRecord`` proportionally based on each asset's *existing* degradation
state.  The existing static readings act as the floor; sensor pressure
amplifies readings toward (but never beyond) their alarm thresholds.

This means:
- Healthy new assets (TX-001) barely move under storm pressure.
- Ageing, already-stressed assets (TX-007, TX-008) approach alarm levels.
- The degradation baseline (age, insulation, overdue maintenance) is never
  changed — only the instantaneous sensor telemetry and weather are affected.

All simulation is deterministic: same inputs → same outputs.

DESIGN CONTRACT
---------------
- Returns a new ``RawAssetRecord`` with modified ``telemetry`` and ``weather``.
- The original record is never mutated.
- All sensor values remain within [healthy_floor, alarm_ceiling].
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Optional

from data.raw_types import RawAssetRecord, RawSensorTelemetry, RawWeatherObservation


# ---------------------------------------------------------------------------
# Sensor alarm ceilings (IEC-based — same as normalisation thresholds)
# ---------------------------------------------------------------------------

_TEMP_HEALTHY_C = 40.0
_TEMP_ALARM_C = 98.0          # top-oil

_HOTSPOT_HEALTHY_C = 50.0
_HOTSPOT_ALARM_C = 128.0

_VIB_HEALTHY_MM_S = 0.5
_VIB_ALARM_MM_S = 4.5

_OIL_HEALTHY_KV = 70.0        # high is healthy (inverted)
_OIL_ALARM_KV = 15.0

_PD_HEALTHY_PC = 100.0
_PD_ALARM_PC = 2000.0

_LOAD_ALARM = 1.0


# ---------------------------------------------------------------------------
# Simulated conditions result
# ---------------------------------------------------------------------------

@dataclass
class SimulatedConditions:
    """The output of ``apply_weather_to_asset``."""
    asset_id: str
    telemetry: RawSensorTelemetry
    weather: RawWeatherObservation
    # 0.0 = no additional pressure; 1.0 = fully pressurised to alarm levels
    effective_pressure: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation: t=0 → a, t=1 → b."""
    return a + (b - a) * max(0.0, min(1.0, t))


def _asset_vulnerability(raw: RawAssetRecord) -> float:
    """
    Compute a 0–1 vulnerability factor for the asset based on its existing
    degradation state.  Healthy new assets have low vulnerability (≈0.1);
    end-of-life critical assets have high vulnerability (≈0.9+).

    This is purely a local weighting function — it does not call the risk
    engine and does not produce a risk score.
    """
    d = raw.degradation
    meta = raw.metadata

    # Age ratio — how far through rated lifespan
    age_ratio = min(1.0, d.age_years / max(1.0, meta.rated_lifespan_years))

    # Insulation health — inverted (low % = high vulnerability)
    ins_score = 0.5  # default when unknown
    if d.insulation_health_pct is not None:
        ins_score = 1.0 - (d.insulation_health_pct / 100.0)

    # Maintenance overdue factor (capped at 180 days)
    overdue_score = min(1.0, d.maintenance_overdue_days / 180.0)

    # Repeat fault flag is a hard multiplier
    repeat_mult = 1.25 if raw.incidents.repeat_mode_flag else 1.0

    base = (age_ratio * 0.40 + ins_score * 0.40 + overdue_score * 0.20)
    return min(1.0, base * repeat_mult)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_weather_to_asset(
    raw: RawAssetRecord,
    simulated_weather: RawWeatherObservation,
    sensor_pressure: float,
) -> SimulatedConditions:
    """
    Derive simulated sensor readings and weather for a single asset.

    The ``sensor_pressure`` (0.0–1.0) from the current simulation phase is
    multiplied by the asset's ``vulnerability`` (also 0–1) to produce an
    ``effective_pressure`` that drives sensor reading amplification.

    Args:
        raw:               Original static asset record (not mutated).
        simulated_weather: Scaled weather observation from ``WeatherSimulator``.
        sensor_pressure:   Phase-level pressure from 0.0 (calm) to 1.0 (peak).

    Returns:
        ``SimulatedConditions`` with updated telemetry and weather.
    """
    t = raw.telemetry
    vuln = _asset_vulnerability(raw)
    effective = min(1.0, sensor_pressure * vuln)

    # ── Temperature ──────────────────────────────────────────────────────────
    base_oil = t.top_oil_temp_c if t.top_oil_temp_c is not None else _TEMP_HEALTHY_C
    new_oil = _lerp(base_oil, _TEMP_ALARM_C, effective * 0.85)

    base_hot = t.winding_hot_spot_c if t.winding_hot_spot_c is not None else _HOTSPOT_HEALTHY_C
    new_hot = _lerp(base_hot, _HOTSPOT_ALARM_C, effective * 0.85)

    # ── Vibration ────────────────────────────────────────────────────────────
    base_vib = t.vibration_mm_s if t.vibration_mm_s is not None else _VIB_HEALTHY_MM_S
    new_vib = _lerp(base_vib, _VIB_ALARM_MM_S, effective * 0.90)

    # ── Oil dielectric (inverted — drops under pressure) ─────────────────────
    base_oil_kv = t.oil_dielectric_kv if t.oil_dielectric_kv is not None else _OIL_HEALTHY_KV
    new_oil_kv = _lerp(base_oil_kv, _OIL_ALARM_KV, effective * 0.90)

    # ── Partial discharge ─────────────────────────────────────────────────────
    base_pd = t.partial_discharge_pc if t.partial_discharge_pc is not None else _PD_HEALTHY_PC
    new_pd = _lerp(base_pd, _PD_ALARM_PC, effective * 0.90)

    # ── Load factor — rises modestly under storm conditions ───────────────────
    base_load = t.load_factor_current if t.load_factor_current is not None else 0.5
    # Load increases slightly as customers draw more power during extreme weather
    new_load = min(_LOAD_ALARM, base_load + effective * 0.10)

    new_telemetry = RawSensorTelemetry(
        top_oil_temp_c=round(new_oil, 1),
        winding_hot_spot_c=round(new_hot, 1),
        vibration_mm_s=round(new_vib, 2),
        oil_dielectric_kv=round(new_oil_kv, 1),
        partial_discharge_pc=round(new_pd, 0),
        load_factor_current=round(new_load, 3),
    )

    return SimulatedConditions(
        asset_id=raw.metadata.asset_id,
        telemetry=new_telemetry,
        weather=simulated_weather,
        effective_pressure=round(effective, 3),
    )
