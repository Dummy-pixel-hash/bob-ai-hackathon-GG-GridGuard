"""
Tests for the simulation layer.

Coverage:
  - WeatherSimulator phase scaling (all 7 phases)
  - WeatherSimulator fallback when network is unavailable
  - WeatherSimulator advance / set_phase / phase_index
  - SimulationPhase enum values
  - apply_weather_to_asset — sensor pressure produces expected direction of change
  - apply_weather_to_asset — zero pressure leaves sensors unchanged
  - apply_weather_to_asset — full pressure drives values toward alarm thresholds
  - apply_weather_to_asset — vulnerability is proportional to degradation state
  - Fallback: simulation outputs are always valid (sensors within bounds)
"""

from __future__ import annotations

import copy
import sys
import os

import pytest

# Ensure src/ is on path
_SRC = os.path.join(os.path.dirname(__file__), "..")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from data.raw_types import (
    AssetLocation,
    AssetMetadata,
    RawAssetRecord,
    RawDegradationRecord,
    RawGridTopology,
    RawIncidentRecord,
    RawSensorTelemetry,
    RawWeatherObservation,
)
from simulation.weather_sim import (
    DEMO_PHASES,
    SimulationPhase,
    WeatherSimulator,
)
from simulation.asset_sim import apply_weather_to_asset, _asset_vulnerability


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_raw(
    asset_id: str = "TX-TEST",
    age_years: float = 10.0,
    insulation_pct: float = 90.0,
    overdue_days: int = 0,
    repeat_fault: bool = False,
    oil_kv: float = 65.0,
    vib: float = 0.6,
    temp: float = 55.0,
    pd: float = 100.0,
    load: float = 0.50,
) -> RawAssetRecord:
    return RawAssetRecord(
        metadata=AssetMetadata(
            asset_id=asset_id,
            asset_type="transformer",
            rated_kva=40_000,
            rated_voltage_kv=66.0,
            rated_lifespan_years=40.0,
            commissioned_year=2014,
            location=AssetLocation(51.5, -0.1, "Test Sub", "Test"),
        ),
        telemetry=RawSensorTelemetry(
            top_oil_temp_c=temp,
            winding_hot_spot_c=temp + 15,
            vibration_mm_s=vib,
            oil_dielectric_kv=oil_kv,
            partial_discharge_pc=pd,
            load_factor_current=load,
        ),
        weather=RawWeatherObservation(max_temp_c=20.0, min_temp_c=10.0),
        incidents=RawIncidentRecord(repeat_mode_flag=repeat_fault),
        degradation=RawDegradationRecord(
            age_years=age_years,
            insulation_health_pct=insulation_pct,
            maintenance_overdue_days=overdue_days,
        ),
        topology=RawGridTopology(customers_served=1000),
    )


_BASE_WEATHER = RawWeatherObservation(
    max_temp_c=38.0,
    min_temp_c=26.0,
    precipitation_mm=82.0,
    wind_speed_max_kmh=115.0,
    storm_warning_level=3,
    forecast_hours=72,
)


# ===========================================================================
# 1.  WeatherSimulator — construction and fallback
# ===========================================================================

class TestWeatherSimulatorConstruction:

    def test_no_live_fetch_uses_static_fallback(self):
        """When attempt_live=False the simulator uses the static TX-007 base."""
        sim = WeatherSimulator(attempt_live=False)
        assert sim.live_base is False
        obs = sim.current_weather()
        assert isinstance(obs, RawWeatherObservation)
        # Static base TX-007 has max_temp=38 °C * CALM mult (0.55) ≈ 20.9
        assert obs.max_temp_c > 0.0

    def test_default_phase_is_calm(self):
        sim = WeatherSimulator(attempt_live=False)
        assert sim.current_phase == SimulationPhase.CALM

    def test_custom_starting_phase(self):
        sim = WeatherSimulator(attempt_live=False, current_phase=SimulationPhase.PEAK_STORM)
        assert sim.current_phase == SimulationPhase.PEAK_STORM


# ===========================================================================
# 2.  WeatherSimulator — phase navigation
# ===========================================================================

class TestWeatherSimulatorPhaseNav:

    def setup_method(self):
        self.sim = WeatherSimulator(attempt_live=False)

    def test_advance_moves_to_next_phase(self):
        assert self.sim.current_phase == SimulationPhase.CALM
        new_phase = self.sim.advance()
        assert new_phase == SimulationPhase.ADVISORY
        assert self.sim.current_phase == SimulationPhase.ADVISORY

    def test_advance_wraps_at_end(self):
        # Advance to the last phase first
        for _ in range(len(DEMO_PHASES) - 1):
            self.sim.advance()
        assert self.sim.phase_index == len(DEMO_PHASES) - 1
        # One more advance wraps back to 0
        self.sim.advance()
        assert self.sim.phase_index == 0

    def test_set_phase_jumps_directly(self):
        self.sim.set_phase(SimulationPhase.DEGRADATION_PEAK)
        assert self.sim.current_phase == SimulationPhase.DEGRADATION_PEAK

    def test_phase_index_is_correct(self):
        for i, p in enumerate(DEMO_PHASES):
            self.sim.set_phase(p.phase)
            assert self.sim.phase_index == i

    def test_phase_info_matches_current(self):
        self.sim.set_phase(SimulationPhase.PEAK_STORM)
        info = self.sim.phase_info
        assert info.phase == SimulationPhase.PEAK_STORM
        assert info.sensor_pressure == pytest.approx(0.55)


# ===========================================================================
# 3.  WeatherSimulator — phase scaling produces expected ordering
# ===========================================================================

class TestWeatherSimulatorScaling:

    def test_calm_produces_lower_values_than_peak_storm(self):
        sim = WeatherSimulator(attempt_live=False)
        sim.set_phase(SimulationPhase.CALM)
        calm = sim.current_weather()
        sim.set_phase(SimulationPhase.PEAK_STORM)
        peak = sim.current_weather()
        assert peak.wind_speed_max_kmh > calm.wind_speed_max_kmh
        assert peak.precipitation_mm > calm.precipitation_mm

    def test_storm_warning_level_increases_with_phase(self):
        sim = WeatherSimulator(attempt_live=False)
        sim.set_phase(SimulationPhase.CALM)
        assert sim.current_weather().storm_warning_level == 0
        sim.set_phase(SimulationPhase.ADVISORY)
        assert sim.current_weather().storm_warning_level == 1
        sim.set_phase(SimulationPhase.PEAK_STORM)
        assert sim.current_weather().storm_warning_level == 3

    def test_all_phases_produce_valid_observations(self):
        sim = WeatherSimulator(attempt_live=False)
        for p in DEMO_PHASES:
            sim.set_phase(p.phase)
            obs = sim.current_weather()
            assert 0 <= obs.storm_warning_level <= 3
            assert obs.precipitation_mm >= 0
            assert obs.wind_speed_max_kmh >= 0
            assert obs.forecast_hours > 0

    def test_recovery_lower_than_peak_storm(self):
        sim = WeatherSimulator(attempt_live=False)
        sim.set_phase(SimulationPhase.RECOVERY)
        recovery = sim.current_weather()
        sim.set_phase(SimulationPhase.PEAK_STORM)
        peak = sim.current_weather()
        assert peak.wind_speed_max_kmh > recovery.wind_speed_max_kmh


# ===========================================================================
# 4.  WeatherSimulator — determinism
# ===========================================================================

class TestWeatherSimulatorDeterminism:

    def test_same_phase_same_output(self):
        sim1 = WeatherSimulator(attempt_live=False)
        sim2 = WeatherSimulator(attempt_live=False)
        for p in DEMO_PHASES:
            sim1.set_phase(p.phase)
            sim2.set_phase(p.phase)
            obs1 = sim1.current_weather()
            obs2 = sim2.current_weather()
            assert obs1.max_temp_c == obs2.max_temp_c
            assert obs1.wind_speed_max_kmh == obs2.wind_speed_max_kmh


# ===========================================================================
# 5.  apply_weather_to_asset — basic behaviour
# ===========================================================================

class TestApplyWeatherToAsset:

    def test_zero_pressure_leaves_sensors_at_base(self):
        raw = _make_raw(temp=55.0, vib=0.6, oil_kv=65.0, pd=100.0, load=0.5)
        cond = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.0)
        t = cond.telemetry
        # With 0 pressure: new = lerp(base, alarm, 0) = base
        assert t.top_oil_temp_c == pytest.approx(55.0, abs=0.2)
        assert t.vibration_mm_s == pytest.approx(0.6, abs=0.1)
        assert t.oil_dielectric_kv == pytest.approx(65.0, abs=0.5)
        assert t.partial_discharge_pc == pytest.approx(100.0, abs=5)

    def test_returns_simulated_conditions_type(self):
        from simulation.asset_sim import SimulatedConditions
        raw = _make_raw()
        cond = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.5)
        assert isinstance(cond, SimulatedConditions)
        assert cond.asset_id == "TX-TEST"

    def test_pressure_increases_temperature(self):
        raw = _make_raw(temp=55.0)
        low = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.1)
        high = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.8)
        assert high.telemetry.top_oil_temp_c > low.telemetry.top_oil_temp_c

    def test_pressure_decreases_oil_dielectric(self):
        raw = _make_raw(oil_kv=65.0)
        low = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.1)
        high = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.8)
        # oil_kv drops under pressure (lower is worse)
        assert high.telemetry.oil_dielectric_kv < low.telemetry.oil_dielectric_kv

    def test_pressure_increases_partial_discharge(self):
        raw = _make_raw(pd=200.0)
        low = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.1)
        high = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.9)
        assert high.telemetry.partial_discharge_pc > low.telemetry.partial_discharge_pc

    def test_all_sensor_values_within_valid_bounds(self):
        """Simulated values must never escape physical limits."""
        raw = _make_raw(temp=91.0, vib=4.0, oil_kv=20.0, pd=1800.0, load=0.9,
                        insulation_pct=15.0, age_years=43.0, overdue_days=200)
        for pressure in [0.0, 0.25, 0.5, 0.75, 1.0]:
            cond = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=pressure)
            t = cond.telemetry
            # Temperature must be below alarm ceiling
            assert t.top_oil_temp_c <= 98.5  # slight tolerance for rounding
            assert t.vibration_mm_s <= 4.55
            assert t.oil_dielectric_kv >= 14.5
            assert t.partial_discharge_pc <= 2010.0
            assert 0.0 <= t.load_factor_current <= 1.0

    def test_original_record_not_mutated(self):
        raw = _make_raw(temp=55.0)
        original_temp = raw.telemetry.top_oil_temp_c
        apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.9)
        assert raw.telemetry.top_oil_temp_c == original_temp

    def test_weather_is_propagated_to_conditions(self):
        raw = _make_raw()
        cond = apply_weather_to_asset(raw, _BASE_WEATHER, sensor_pressure=0.0)
        assert cond.weather.storm_warning_level == _BASE_WEATHER.storm_warning_level


# ===========================================================================
# 6.  Asset vulnerability — proportional to degradation
# ===========================================================================

class TestAssetVulnerability:

    def test_new_healthy_asset_has_low_vulnerability(self):
        raw = _make_raw(age_years=2.0, insulation_pct=98.0, overdue_days=0)
        v = _asset_vulnerability(raw)
        assert v < 0.4

    def test_old_degraded_asset_has_high_vulnerability(self):
        raw = _make_raw(age_years=43.0, insulation_pct=12.0, overdue_days=200, repeat_fault=True)
        v = _asset_vulnerability(raw)
        assert v >= 0.7

    def test_repeat_fault_increases_vulnerability(self):
        base = _make_raw(age_years=30.0, insulation_pct=50.0)
        with_repeat = _make_raw(age_years=30.0, insulation_pct=50.0, repeat_fault=True)
        assert _asset_vulnerability(with_repeat) > _asset_vulnerability(base)

    def test_overdue_maintenance_increases_vulnerability(self):
        on_schedule = _make_raw(overdue_days=0)
        overdue = _make_raw(overdue_days=180)
        assert _asset_vulnerability(overdue) > _asset_vulnerability(on_schedule)

    def test_vulnerability_clamped_to_0_1(self):
        raw = _make_raw(age_years=100.0, insulation_pct=0.0, overdue_days=9999, repeat_fault=True)
        v = _asset_vulnerability(raw)
        assert 0.0 <= v <= 1.0
