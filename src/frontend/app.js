/* GridGuard control-room frontend — vanilla JS, no dependencies.
   Every number rendered from the backend API arrives from the live risk
   engine. buildMockData() below is only a fallback when the backend is
   unreachable: it mirrors the backend response shape and replays the
   staged static demo scenario's engine outputs (same scores the backend
   serves by default). It is clearly labelled "Preview data" in the UI. */
"use strict";

const S = {
  assets: [], summary: null, priorities: null, briefing: null,
  selected: null, sideTab: "overview", currentView: "overview",
  mapZoom: 1,
  filters: { q: "", status: "", region: "", type: "" },
  chatBooted: false, asking: false, history: [],
};

const COLORS = { Healthy: "#187245", Monitoring: "#8a6a0c", High: "#b45309", Critical: "#b42318" };
const ICON_TX = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M8 4.5h3M13 4.5h3M9.5 4.5V8M14.5 4.5V8"/><rect x="6.5" y="8" width="11" height="10" rx="1.5"/><path d="M4 10.5h2.5M4 13h2.5M4 15.5h2.5M17.5 10.5h2.5M17.5 13h2.5M17.5 15.5h2.5M5.5 21h13"/></svg>`;
const ICON_SUB = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M12 3.5V6M10.9 8.5L12 6L13.1 8.5"/><path d="M9.2 21L11 8.5M14.8 21L13 8.5"/><path d="M8 10.5h8M6.8 14.5h10.4M9.7 18.5h4.6"/><path d="M9.5 10.5v2.5M14.5 10.5v2.5M8.3 14.5v2.5M15.7 14.5v2.5"/></svg>`;
const TYPE_ICON = { transformer: ICON_TX, substation: ICON_SUB };
/* Inline SVG icons for nav tabs and UI elements — consistent, accessible, no external dep */
const NAV_ICON_OVERVIEW = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 6.5L8 2l6 4.5V14H10v-3H6v3H2z"/></svg>`;
const NAV_ICON_ASSETS  = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1.5" y="2.5" width="13" height="2.5" rx=".8"/><rect x="1.5" y="6.75" width="13" height="2.5" rx=".8"/><rect x="1.5" y="11" width="13" height="2.5" rx=".8"/></svg>`;
const NAV_ICON_MAINT   = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.6 2.4a4 4 0 1 0 4 4 4 4 0 0 0-4-4zM6.1 9.9 2 14"/><circle cx="9.6" cy="6.4" r="1.4"/></svg>`;
const NAV_ICON_AI      = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13z"/><path d="M5.5 9.5s.8 1.5 2.5 1.5 2.5-1.5 2.5-1.5M6 6.3h.01M10 6.3h.01"/></svg>`;
const SVG_CREW         = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:inline;vertical-align:-.15em;margin-right:4px"><path d="M8 1.5a3 3 0 1 1 0 6 3 3 0 0 1 0-6zM2 14c0-3.3 2.7-5 6-5s6 1.7 6 5"/></svg>`;
const POS = {
  "TX-001": [11, 17], "TX-002": [33, 15], "TX-003": [58, 16], "TX-004": [82, 16],
  "TX-005": [22, 46], "TX-006": [50, 40], "TX-007": [76, 52], "TX-008": [40, 71],
};
const EDGES = [[0,1],[1,2],[2,3],[1,4],[4,5],[2,5],[5,6],[3,6],[5,7],[4,7],[0,4],[6,7]];
const ORDER = ["TX-001","TX-002","TX-003","TX-004","TX-005","TX-006","TX-007","TX-008"];

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? "—" : v).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtInt = (n) => n == null ? "—" : Number(n).toLocaleString("en-US");
const riskClass = (v) => v >= 85 ? "bad" : v >= 70 ? "bad" : v >= 40 ? "warn" : "ok";
const barColor = (v) => v >= 85 ? COLORS.Critical : v >= 70 ? COLORS.High : v >= 40 ? COLORS.Monitoring : COLORS.Healthy;

/* Offline fallback: same response shape as GET /api/assets, replaying the
   staged static demo scenario's engine outputs (2 Healthy / 2 Monitoring /
   2 High / 2 Critical). Only used when the backend is unreachable — the
   UI banners it as "Preview data". */
function buildMockData() {
  const component_labels = { sensor_health: "Sensor health", weather_risk: "Weather risk", historical_failure: "Historical failure", asset_degradation: "Asset degradation", grid_impact: "Grid impact" };
  const component_weights = { sensor_health: 0.30, weather_risk: 0.20, historical_failure: 0.15, asset_degradation: 0.15, grid_impact: 0.20 };
  const ACTIONS = {
    Healthy: ["Routine monitoring", "No action — next scheduled check"],
    Monitoring: ["Plan inspection", "Routine — schedule within 30 days"],
    High: ["Inspect this week", "Priority — schedule within 7 days"],
    Critical: ["Inspect today", "Immediate — dispatch crew within 24h"],
  };
  const A = (o) => {
    const [recommended_action, action_detail] = ACTIONS[o.status];
    return {
      ...o, component_labels, component_weights, recommended_action, action_detail,
      risk_level: o.status === "Healthy" ? "Normal" : o.status === "Monitoring" ? "Watch" : o.status,
      dominant_factor_label: component_labels[o.dominant_factor],
      lineage: { predecessor: null, successor_of_retired: null },
    };
  };
  const assets = [
    A({
      id: "TX-001", status: "Healthy", asset_type: "transformer", substation: "Riverside Main", region: "Central",
      overall_risk: 3.9, dominant_factor: "weather_risk", commissioned_year: 2015, rated_kva: 40000, rated_voltage_kv: 66,
      components: { sensor_health: 3.1, weather_risk: 9.8, historical_failure: 0, asset_degradation: 1.4, grid_impact: 4.2 },
      sensors_raw: { top_oil_temp_c: 52, winding_hot_spot_c: 68, vibration_mm_s: 0.6, oil_dielectric_kv: 68, partial_discharge_pc: 80, load_factor_current: 0.42 },
      sensors_norm: { temperature_score: 0, vibration_score: 0, oil_quality_score: 5, partial_discharge_score: 0, load_score: 42, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 18, min_temp_c: 10, precipitation_mm: 2, wind_speed_max_kmh: 25, storm_warning_level: 0, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 0, precipitation_score: 2, wind_storm_score: 20.83, forecast_hours: 72 },
      grid_impact: { customers_served: 3200, critical_facility_count: 0, critical_facility_names: [], peak_load_mw: 3.5, downstream_asset_count: 2, has_redundant_path: true },
      lifecycle: { state: "active", age_years: 10, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 30, insulation_health_pct: 96, cumulative_fault_events: 0, maintenance_overdue_days: 0, average_load_factor: 0.42, failure_count_last_5yr: 0, failures_caused_by_weather: 0, last_failure_days_ago: null, repeat_fault_active: false, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 10, maintenance_overdue_days: 0, insulation_health_pct: 96, cumulative_fault_events: 0 },
      history: { failure_count_last_5yr: 0, failures_caused_by_weather: 0, last_failure_days_ago: null, repeat_mode_flag: false, mean_time_between_failures_days: null },
    }),
    A({
      id: "TX-002", status: "Healthy", asset_type: "transformer", substation: "Northgate", region: "North",
      overall_risk: 8.8, dominant_factor: "sensor_health", commissioned_year: 2007, rated_kva: 25000, rated_voltage_kv: 33,
      components: { sensor_health: 10, weather_risk: 12.2, historical_failure: 0, asset_degradation: 12.2, grid_impact: 7.4 },
      sensors_raw: { top_oil_temp_c: 58, winding_hot_spot_c: 74, vibration_mm_s: 0.9, oil_dielectric_kv: 62, partial_discharge_pc: 150, load_factor_current: 0.38 },
      sensors_norm: { temperature_score: 7, vibration_score: 0, oil_quality_score: 20, partial_discharge_score: 5.6, load_score: 38, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 22, min_temp_c: 12, precipitation_mm: 5, wind_speed_max_kmh: 30, storm_warning_level: 0, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 0, precipitation_score: 5, wind_storm_score: 25, forecast_hours: 72 },
      grid_impact: { customers_served: 7500, critical_facility_count: 0, critical_facility_names: [], peak_load_mw: 6, downstream_asset_count: 3, has_redundant_path: true },
      lifecycle: { state: "active", age_years: 18, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 22, insulation_health_pct: 88, cumulative_fault_events: 1, maintenance_overdue_days: 0, average_load_factor: 0.4, failure_count_last_5yr: 0, failures_caused_by_weather: 0, last_failure_days_ago: null, repeat_fault_active: false, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 18, maintenance_overdue_days: 0, insulation_health_pct: 88, cumulative_fault_events: 1 },
      history: { failure_count_last_5yr: 0, failures_caused_by_weather: 0, last_failure_days_ago: null, repeat_mode_flag: false, mean_time_between_failures_days: null },
    }),
    A({
      id: "TX-003", status: "Monitoring", asset_type: "transformer", substation: "Eastside Industrial", region: "East",
      overall_risk: 47.9, dominant_factor: "sensor_health", commissioned_year: 1997, rated_kva: 60000, rated_voltage_kv: 110,
      components: { sensor_health: 58.3, weather_risk: 74.7, historical_failure: 30, asset_degradation: 51, grid_impact: 16.4 },
      sensors_raw: { top_oil_temp_c: 80, winding_hot_spot_c: 103, vibration_mm_s: 2.2, oil_dielectric_kv: 34, partial_discharge_pc: 480, load_factor_current: 0.78 },
      sensors_norm: { temperature_score: 58.1, vibration_score: 34.3, oil_quality_score: 90, partial_discharge_score: 42.2, load_score: 78, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 36, min_temp_c: 24, precipitation_mm: 28, wind_speed_max_kmh: 65, storm_warning_level: 2, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 55, precipitation_score: 28, wind_storm_score: 89.17, forecast_hours: 72 },
      grid_impact: { customers_served: 18000, critical_facility_count: 0, critical_facility_names: [], peak_load_mw: 22, downstream_asset_count: 5, has_redundant_path: true },
      lifecycle: { state: "active", age_years: 28, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 12, insulation_health_pct: 65, cumulative_fault_events: 4, maintenance_overdue_days: 55, average_load_factor: 0.72, failure_count_last_5yr: 1, failures_caused_by_weather: 1, last_failure_days_ago: 425, repeat_fault_active: false, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 28, maintenance_overdue_days: 55, insulation_health_pct: 65, cumulative_fault_events: 4 },
      history: { failure_count_last_5yr: 1, failures_caused_by_weather: 1, last_failure_days_ago: 425, repeat_mode_flag: false, mean_time_between_failures_days: null },
    }),
    A({
      id: "TX-004", status: "Monitoring", asset_type: "transformer", substation: "Central Business", region: "Central",
      overall_risk: 45.9, dominant_factor: "weather_risk", commissioned_year: 2003, rated_kva: 50000, rated_voltage_kv: 66,
      components: { sensor_health: 52.3, weather_risk: 80.2, historical_failure: 21, asset_degradation: 48.4, grid_impact: 18.6 },
      sensors_raw: { top_oil_temp_c: 78, winding_hot_spot_c: 100, vibration_mm_s: 1.8, oil_dielectric_kv: 38, partial_discharge_pc: 450, load_factor_current: 0.72 },
      sensors_norm: { temperature_score: 53.5, vibration_score: 22.9, oil_quality_score: 80, partial_discharge_score: 38.9, load_score: 72, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 37, min_temp_c: 26, precipitation_mm: 30, wind_speed_max_kmh: 72, storm_warning_level: 2, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 60, precipitation_score: 30, wind_storm_score: 95, forecast_hours: 72 },
      grid_impact: { customers_served: 22000, critical_facility_count: 0, critical_facility_names: [], peak_load_mw: 26, downstream_asset_count: 5, has_redundant_path: true },
      lifecycle: { state: "active", age_years: 22, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 18, insulation_health_pct: 68, cumulative_fault_events: 4, maintenance_overdue_days: 60, average_load_factor: 0.68, failure_count_last_5yr: 1, failures_caused_by_weather: 0, last_failure_days_ago: 185, repeat_fault_active: false, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 22, maintenance_overdue_days: 60, insulation_health_pct: 68, cumulative_fault_events: 4 },
      history: { failure_count_last_5yr: 1, failures_caused_by_weather: 0, last_failure_days_ago: 185, repeat_mode_flag: false, mean_time_between_failures_days: null },
    }),
    A({
      id: "TX-005", status: "High", asset_type: "substation", substation: "Harbour Substation", region: "South",
      overall_risk: 70.7, dominant_factor: "sensor_health", commissioned_year: 1991, rated_kva: 80000, rated_voltage_kv: 110,
      components: { sensor_health: 77.5, weather_risk: 84.5, historical_failure: 63, asset_degradation: 72.2, grid_impact: 51.2 },
      sensors_raw: { top_oil_temp_c: 86, winding_hot_spot_c: 112, vibration_mm_s: 2.9, oil_dielectric_kv: 28, partial_discharge_pc: 780, load_factor_current: 0.83 },
      sensors_norm: { temperature_score: 72.1, vibration_score: 54.3, oil_quality_score: 100, partial_discharge_score: 75.6, load_score: 83, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 35, min_temp_c: 24, precipitation_mm: 55, wind_speed_max_kmh: 88, storm_warning_level: 2, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 50, precipitation_score: 55, wind_storm_score: 100, forecast_hours: 72 },
      grid_impact: { customers_served: 28000, critical_facility_count: 1, critical_facility_names: ["Port Authority Control Centre"], peak_load_mw: 28, downstream_asset_count: 7, has_redundant_path: false },
      lifecycle: { state: "active", age_years: 34, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 6, insulation_health_pct: 41, cumulative_fault_events: 7, maintenance_overdue_days: 110, average_load_factor: 0.79, failure_count_last_5yr: 2, failures_caused_by_weather: 2, last_failure_days_ago: 62, repeat_fault_active: false, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 34, maintenance_overdue_days: 110, insulation_health_pct: 41, cumulative_fault_events: 7 },
      history: { failure_count_last_5yr: 2, failures_caused_by_weather: 2, last_failure_days_ago: 62, repeat_mode_flag: false, mean_time_between_failures_days: 480 },
    }),
    A({
      id: "TX-006", status: "High", asset_type: "transformer", substation: "Hillcrest", region: "West",
      overall_risk: 72.3, dominant_factor: "sensor_health", commissioned_year: 1994, rated_kva: 35000, rated_voltage_kv: 33,
      components: { sensor_health: 79.7, weather_risk: 82.5, historical_failure: 75.5, asset_degradation: 72.9, grid_impact: 48 },
      sensors_raw: { top_oil_temp_c: 85, winding_hot_spot_c: 112, vibration_mm_s: 3.5, oil_dielectric_kv: 29, partial_discharge_pc: 820, load_factor_current: 0.82 },
      sensors_norm: { temperature_score: 69.8, vibration_score: 71.4, oil_quality_score: 100, partial_discharge_score: 80, load_score: 82, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 34, min_temp_c: 22, precipitation_mm: 55, wind_speed_max_kmh: 90, storm_warning_level: 3, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 45, precipitation_score: 55, wind_storm_score: 100, forecast_hours: 72 },
      grid_impact: { customers_served: 28000, critical_facility_count: 1, critical_facility_names: ["Hillcrest Fire Station"], peak_load_mw: 24, downstream_asset_count: 6, has_redundant_path: false },
      lifecycle: { state: "active", age_years: 31, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 9, insulation_health_pct: 38, cumulative_fault_events: 8, maintenance_overdue_days: 120, average_load_factor: 0.78, failure_count_last_5yr: 2, failures_caused_by_weather: 1, last_failure_days_ago: 45, repeat_fault_active: true, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 31, maintenance_overdue_days: 120, insulation_health_pct: 38, cumulative_fault_events: 8 },
      history: { failure_count_last_5yr: 2, failures_caused_by_weather: 1, last_failure_days_ago: 45, repeat_mode_flag: true, mean_time_between_failures_days: 600 },
    }),
    A({
      id: "TX-007", status: "Critical", asset_type: "substation", substation: "Waterfront Plaza", region: "East",
      overall_risk: 95.4, dominant_factor: "sensor_health", commissioned_year: 1982, rated_kva: 120000, rated_voltage_kv: 132,
      components: { sensor_health: 95.6, weather_risk: 96.8, historical_failure: 100, asset_degradation: 91, grid_impact: 93.8 },
      sensors_raw: { top_oil_temp_c: 94, winding_hot_spot_c: 124, vibration_mm_s: 4.1, oil_dielectric_kv: 18, partial_discharge_pc: 2200, load_factor_current: 0.91 },
      sensors_norm: { temperature_score: 92, vibration_score: 88.6, oil_quality_score: 100, partial_discharge_score: 100, load_score: 91, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 38, min_temp_c: 26, precipitation_mm: 82, wind_speed_max_kmh: 115, storm_warning_level: 3, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 65, precipitation_score: 82, wind_storm_score: 100, forecast_hours: 72 },
      grid_impact: { customers_served: 46000, critical_facility_count: 3, critical_facility_names: ["City General Hospital", "Waterfront Water Treatment", "Fire Station HQ"], peak_load_mw: 44, downstream_asset_count: 9, has_redundant_path: false },
      lifecycle: { state: "active", age_years: 43, rated_lifespan_years: 40, past_rated_lifespan: true, remaining_life_years: -3, insulation_health_pct: 12, cumulative_fault_events: 13, maintenance_overdue_days: 210, average_load_factor: 0.88, failure_count_last_5yr: 4, failures_caused_by_weather: 3, last_failure_days_ago: 22, repeat_fault_active: true, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 43, maintenance_overdue_days: 210, insulation_health_pct: 12, cumulative_fault_events: 13 },
      history: { failure_count_last_5yr: 4, failures_caused_by_weather: 3, last_failure_days_ago: 22, repeat_mode_flag: true, mean_time_between_failures_days: 365 },
    }),
    A({
      id: "TX-008", status: "Critical", asset_type: "transformer", substation: "Airport Grid Node", region: "West",
      overall_risk: 87.1, dominant_factor: "sensor_health", commissioned_year: 1987, rated_kva: 100000, rated_voltage_kv: 132,
      components: { sensor_health: 92.5, weather_risk: 90, historical_failure: 93, asset_degradation: 82, grid_impact: 75.4 },
      sensors_raw: { top_oil_temp_c: 91, winding_hot_spot_c: 121, vibration_mm_s: 3.8, oil_dielectric_kv: 22, partial_discharge_pc: 1850, load_factor_current: 0.89 },
      sensors_norm: { temperature_score: 86, vibration_score: 80, oil_quality_score: 100, partial_discharge_score: 100, load_score: 89, missing_sensor_ratio: 0 },
      weather_raw: { max_temp_c: 36, min_temp_c: 24, precipitation_mm: 70, wind_speed_max_kmh: 105, storm_warning_level: 3, forecast_hours: 72 },
      weather_norm: { temperature_stress_score: 55, precipitation_score: 70, wind_storm_score: 100, forecast_hours: 72 },
      grid_impact: { customers_served: 38000, critical_facility_count: 2, critical_facility_names: ["Airport Control Tower", "Airport Fuel Pumping Station"], peak_load_mw: 42, downstream_asset_count: 8, has_redundant_path: false },
      lifecycle: { state: "active", age_years: 38, rated_lifespan_years: 40, past_rated_lifespan: false, remaining_life_years: 2, insulation_health_pct: 20, cumulative_fault_events: 10, maintenance_overdue_days: 175, average_load_factor: 0.85, failure_count_last_5yr: 3, failures_caused_by_weather: 2, last_failure_days_ago: 30, repeat_fault_active: true, fault_history: [], maintenance_history: [], predecessor_asset_id: null },
      degradation: { age_years: 38, maintenance_overdue_days: 175, insulation_health_pct: 20, cumulative_fault_events: 10 },
      history: { failure_count_last_5yr: 3, failures_caused_by_weather: 2, last_failure_days_ago: 30, repeat_mode_flag: true, mean_time_between_failures_days: 480 },
    }),
  ];

  const counts = { Healthy: 0, Monitoring: 0, High: 0, Critical: 0 };
  assets.forEach((a) => { counts[a.status] += 1; });

  const summary = {
    total: assets.length, counts,
    average_risk: 54, highest_risk: 95.4,
    customers_at_risk: 140000, critical_facilities_exposed: 7,
    regions: {
      Central: { assets: 2, worst_risk: 45.9, worst_status: "Monitoring" },
      North: { assets: 1, worst_risk: 8.8, worst_status: "Healthy" },
      East: { assets: 2, worst_risk: 95.4, worst_status: "Critical" },
      South: { assets: 1, worst_risk: 70.7, worst_status: "High" },
      West: { assets: 2, worst_risk: 87.1, worst_status: "Critical" },
    },
  };
  const ranked = [...assets].sort((a, b) => b.overall_risk - a.overall_risk);
  const priorities = {
    generated_at: new Date().toISOString(),
    maintenance_plan: ranked.map((a, index) => ({
      rank: index + 1, asset_id: a.id, substation: a.substation, region: a.region,
      status: a.status, overall_risk: a.overall_risk,
      dominant_factor: a.dominant_factor, dominant_factor_label: a.dominant_factor_label,
      customers_served: a.grid_impact.customers_served,
      critical_facilities: a.grid_impact.critical_facility_count,
      has_redundant_path: a.grid_impact.has_redundant_path,
      recommended_action: a.recommended_action, action_detail: a.action_detail,
    })),
    crew_prepositioning: [
      { region: "East", assets: ["TX-007"], reason: "Storm exposure (weather 96.8/100, warning level 3) on TX-007 (Critical, 95.4/100) — stage crews in East before the front arrives." },
      { region: "West", assets: ["TX-006", "TX-008"], reason: "Storm exposure (weather 90/100, warning level 3) on TX-008 (Critical, 87.1/100) — stage crews in West before the front arrives." },
      { region: "South", assets: ["TX-005"], reason: "Storm exposure (weather 84.5/100, warning level 2) on TX-005 (High, 70.7/100) — stage crews in South before the front arrives." },
    ],
  };

  return {
    assets,
    summary,
    priorities,
    briefing: {
      provider: "mock",
      model: "offline-mock",
      offline: true,
      warning: "Preview mode active. Backend is not running.",
      env_files: [],
    },
  };
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

/* ---------------- data ---------------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

async function init() {
  try {
    const [d1, d2, d3, d4] = await Promise.all([
      api("/api/assets"), api("/api/summary"), api("/api/priorities"), api("/api/briefing_info"),
    ]);
    S.assets = d1.assets; S.summary = d2; S.priorities = d3; S.briefing = d4;
  } catch (e) {
    const mock = buildMockData();
    S.assets = mock.assets; S.summary = mock.summary; S.priorities = mock.priorities; S.briefing = mock.briefing;
  }
  const worst = [...S.assets].sort((a, b) => b.overall_risk - a.overall_risk)[0];
  S.selected = worst ? worst.id : null;
  S._lastRisk = riskSnapshot(S.assets);
  buildFilters(); renderAll(); startClock(); badge();
}

function buildFilters() {
  const regions = [...new Set(S.assets.map((a) => a.region))].sort();
  const types = [...new Set(S.assets.map((a) => a.asset_type))].sort();
  $("f-region").innerHTML = `<option value="">All Regions</option>` + regions.map((r) => `<option>${esc(r)}</option>`).join("");
  $("f-type").innerHTML = `<option value="">All Types</option>` + types.map((t) => `<option value="${esc(t)}">${esc(cap(t))}s</option>`).join("");
  $("ai-asset").innerHTML = `<option value="">Fleet-wide</option>` + S.assets.map((a) => `<option value="${a.id}">${a.id} · ${esc(a.substation)}</option>`).join("");
}
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;

function filtered() {
  const f = S.filters;
  return S.assets.filter((a) =>
    (!f.status || a.status === f.status) &&
    (!f.region || a.region === f.region) &&
    (!f.type || a.asset_type === f.type) &&
    (!f.q || (a.id + " " + a.substation + " " + a.region).toLowerCase().includes(f.q))
  );
}

/* ---------------- KPIs ---------------- */
function renderKPIs() {
  const list = filtered();
  const c = { Healthy: 0, Monitoring: 0, High: 0, Critical: 0 };
  list.forEach((a) => { c[a.status] += 1; });
  const kpi = (cls, icon, n, label, st) =>
    `<button type="button" class="kpi ${cls}" data-status="${st}" aria-pressed="${S.filters.status === st}" title="Filter by ${label}"><span class="ico" aria-hidden="true">${icon}</span><span><span class="n">${n}</span><br><span class="l">${label}</span></span></button>`;
  $("kpis").innerHTML =
    kpi("total", "◔", list.length, "Total Assets", "") +
    kpi("healthy", "◉", c.Healthy, "Healthy", "Healthy") +
    kpi("monitoring", "◉", c.Monitoring, "Monitoring", "Monitoring") +
    kpi("high", "⬢", c.High, "High", "High") +
    kpi("critical", "⬢", c.Critical, "Critical", "Critical");
  $("kpis").querySelectorAll(".kpi").forEach((b) => b.onclick = () => {
    // Clicking the active status filter toggles it back off.
    S.filters.status = (b.dataset.status && S.filters.status !== b.dataset.status) ? b.dataset.status : "";
    $("f-status").value = S.filters.status;
    refreshFiltered();
  });
}

function filtersActive() {
  const f = S.filters;
  return Boolean(f.q || f.status || f.region || f.type);
}

function renderFilterMeta() {
  const el = $("filter-meta");
  if (!el) return;
  if (!filtersActive()) { el.hidden = true; return; }
  const n = filtered().length, m = S.assets.length;
  el.hidden = false;
  el.innerHTML = `<span>Showing <b>${n}</b> of ${m} assets</span><button type="button" class="linklike" id="filter-clear">Clear filters</button>`;
  $("filter-clear").onclick = clearFilters;
}

function clearFilters() {
  S.filters = { q: "", status: "", region: "", type: "" };
  $("q").value = ""; $("f-status").value = ""; $("f-region").value = ""; $("f-type").value = "";
  refreshFiltered();
}

function refreshFiltered() {
  renderKPIs(); renderMap(); renderQueue(); renderStrips();
  renderAssetsTable(); renderFilterMeta();
}

/* ---------------- network map ---------------- */
function nodeEl(id) {
  return document.querySelector(`#nodes .node[data-node-id="${id}"]`);
}

const riskSnapshot = (assets) => Object.fromEntries(assets.map((a) => [a.id, a.overall_risk]));

/* An asset is "deteriorating" while the simulation is live and its engine
   risk keeps climbing between polls — pure visualisation of backend truth. */
function isDeteriorating(a) {
  if (!SIM.active || SIM.paused || SIM.completed) return false;
  const prev = S._lastRisk ? S._lastRisk[a.id] : undefined;
  return prev != null && a.overall_risk > prev + 0.05;
}

/* In-place map update: nodes are created once and then updated (status band,
   risk value, selection, deteriorating halo, one-shot change flashes) so a
   simulation refresh never replays the entrance animation or loses context. */
function renderMap() {
  const box = $("nodes");
  const vis = new Set(filtered().map((a) => a.id));
  const byId = new Map(S.assets.map((a) => [a.id, a]));
  // Exactly one live marker per asset survives: drop unknown ids and
  // 2nd+ duplicates (keep the first), and heal any stray missing its base.
  const seen = new Set();
  box.querySelectorAll("[data-node-id]").forEach((n) => {
    const nid = n.dataset.nodeId;
    if (!byId.has(nid) || seen.has(nid)) { n.remove(); return; }
    seen.add(nid);
    if (!n.classList.contains("node")) n.classList.add("node");
  });
  ORDER.filter((id) => byId.has(id)).forEach((id, i) => {
    const a = byId.get(id);
    let el = box.querySelector(`.node[data-node-id="${id}"]`);
    if (!el) {
      el = document.createElement("button");
      el.type = "button";
      el.className = "node"; // base class FIRST — dedup + positioning + flash layers depend on it
      el.dataset.nodeId = id;
      el.style.animationDelay = (i * 0.05) + "s";
      el.onclick = () => selectNode(id);
      el.ondblclick = () => openModal(id);
      box.appendChild(el);
    }
    el.style.display = vis.has(id) ? "" : "none";
    const [x, y] = POS[id] || [10 + i * 10, 50];
    el.style.left = x + "%"; el.style.top = y + "%";
    el.classList.toggle("st-Healthy", a.status === "Healthy");
    el.classList.toggle("st-Monitoring", a.status === "Monitoring");
    el.classList.toggle("st-High", a.status === "High");
    el.classList.toggle("st-Critical", a.status === "Critical");
    el.classList.toggle("pulse", a.status === "Critical");
    el.classList.toggle("selected", S.selected === id);
    el.classList.toggle("deteriorating", isDeteriorating(a));
    el.title = `${a.id} · ${a.status} · ${a.overall_risk}/100`;
    el.setAttribute("aria-label", `${a.id}, ${a.asset_type}, ${a.status}, risk ${a.overall_risk} of 100. Activate to inspect.`);
    el.setAttribute("aria-pressed", S.selected === id ? "true" : "false");
    el.innerHTML = `<span class="nico" aria-hidden="true">${TYPE_ICON[a.asset_type] || ICON_TX}</span>
      <span><span class="nid">${esc(a.id)}</span><br><span class="ntype">${esc(cap(a.asset_type))}</span></span>
      <span class="nrisk" style="color:${barColor(a.overall_risk)}">${a.overall_risk}</span>
      <span class="ndot" aria-hidden="true"></span>`;
    // consume queued one-shot flashes from the latest backend change events
    const f = SIM.flash && SIM.flash[id];
    if (f) {
      ["flash-changed", "flash-escalated", "flash-improved"].forEach((c) => el.classList.remove(c));
      void el.offsetWidth; // restart the one-shot animation
      el.classList.add(f);
      delete SIM.flash[id];
      setTimeout(() => { const n = nodeEl(id); if (n) n.classList.remove(f); }, 2400);
    }
  });
  const anyVis = ORDER.some((id) => vis.has(id) && byId.has(id));
  let empty = box.querySelector(".map-empty");
  if (!anyVis) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "map-empty";
      box.appendChild(empty);
    }
    empty.textContent = "No assets match the current filters.";
  } else if (empty) {
    empty.remove();
  }
  drawNetlines();
}

/* Static topology dressing — built once; zoom is a pure CSS transform. */
function drawNetlines() {
  const svg = $("netlines");
  if (svg.dataset.built === "1") return;
  svg.dataset.built = "1";
  const pts = ORDER.map((id) => POS[id]);
  svg.setAttribute("viewBox", "-35 -35 170 170");
  const streets = buildStreets();
  const lines = EDGES.filter(([a, b]) => pts[a] && pts[b]).map(([a, b]) => {
    return `<line x1="${pts[a][0]}" y1="${pts[a][1]}" x2="${pts[b][0]}" y2="${pts[b][1]}"
      stroke="#7f957a" stroke-opacity="0.7" stroke-width="1.25" vector-effect="non-scaling-stroke"/>`;
  }).join("");
  svg.innerHTML = streets + `<g>${lines}</g>`;
}

/* Selection updates the existing nodes in place so the rest of the map
   stays static — no rebuild, no replayed entrance animation. */
function selectNode(id) {
  S.selected = id;
  document.querySelectorAll("#nodes .node").forEach((n) => {
    const on = n.dataset.nodeId === id;
    n.classList.toggle("selected", on);
    n.setAttribute("aria-pressed", on ? "true" : "false");
  });
  renderSide();
}

function setMapZoom(next) {
  S.mapZoom = Math.min(1.6, Math.max(0.8, next));
  $("map-canvas").style.transform = `scale(${S.mapZoom})`;
  $("map-zoom-level").textContent = `${Math.round(S.mapZoom * 100)}%`;
}

/* Quiet map canvas: five major roads across the bleed area so zooming never
   exposes bare canvas. No minor fabric — the data stays the hierarchy.
   Pure dressing; all data rides on top of it. */
function buildStreets() {
  let s = `<g fill="none" vector-effect="non-scaling-stroke" aria-hidden="true">`;
  // Three arterials: straight diagonal connectors across the bleed
  [["M30,-60 L60,160", 2.5], ["M-45,20 L145,58", 2.5], ["M-45,90 L145,26", 2.5]].forEach(([d, w]) => {
    s += `<path d="${d}" stroke="#ffffff" stroke-width="${w}" stroke-opacity="0.95"/>`;
  });
  // Two highways: dark casing + white fill, smooth beziers
  [["M-45,74 C30,62 65,84 145,42"], ["M15,-45 C36,42 58,74 95,145"]].forEach(([d]) => {
    s += `<path d="${d}" stroke="#c6c6c6" stroke-width="6"/><path d="${d}" stroke="#ffffff" stroke-width="4"/>`;
  });
  return s + `</g>`;
}

/* ---------------- side panel ---------------- */
function ringSVG(score, extraCls) {
  const C = 2 * Math.PI * 44, off = C * (1 - score / 100);
  return `<div class="ring${extraCls || ""}"><svg width="104" height="104" viewBox="0 0 104 104">
    <circle cx="52" cy="52" r="44" fill="none" stroke="#e3e9f0" stroke-width="10"/>
    <circle cx="52" cy="52" r="44" fill="none" stroke="${barColor(score)}" stroke-width="10"
      stroke-linecap="round" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/></svg>
    <span class="rv"><span><b>${score}</b><br><small>/100</small></span></span></div>`;
}

function renderSide() {
  const a = S.assets.find((x) => x.id === S.selected);
  const el = $("side-panel");
  if (!a) { el.innerHTML = `<p style="color:var(--muted)">Select an asset on the grid.</p>`; return; }
  const s = a.sensors_raw, gi = a.grid_impact;
  // Live-change tracking: snapshot this render's key values and compare
  // against the previous render — components whose backend values moved get
  // a brief highlight so the operator sees the side panel react.
  const w = a.weather_raw;
  const cur = {
    status: a.status, risk: a.overall_risk,
    temp: s.top_oil_temp_c, hotspot: s.winding_hot_spot_c, vib: s.vibration_mm_s,
    oil: s.oil_dielectric_kv, pd: s.partial_discharge_pc, load: s.load_factor_current,
    wmax: w.max_temp_c, wmin: w.min_temp_c,
    precip: w.precipitation_mm, wind: w.wind_speed_max_kmh,
  };
  const prev = (S._lastSideVals && S._lastSideVals[a.id]) || {};
  const ch = (key) => (prev[key] !== undefined && prev[key] !== cur[key]) ? " just-changed" : "";
  S._lastSideVals = { ...(S._lastSideVals || {}), [a.id]: cur };
  const tabs = ["overview", "sensors", "weather", "history"]
    .map((t) => `<button data-t="${t}" class="${S.sideTab === t ? "active" : ""}">${cap(t === "sensors" ? "Sensor Data" : t)}</button>`).join("");
  let body = "";
  if (S.sideTab === "overview") {
    body = `<div class="ring-row">${ringSVG(a.overall_risk, ch("risk"))}
      <div class="sensor-rows">
        <div class="sr"><span>Temperature</span><span class="val ${riskClass(a.sensors_norm.temperature_score)}${ch("temp")}">${s.top_oil_temp_c} °C</span></div>
        <div class="sr"><span>Vibration</span><span class="val ${riskClass(a.sensors_norm.vibration_score)}${ch("vib")}">${vibLabel(a)}</span></div>
        <div class="sr"><span>Oil Quality</span><span class="val ${riskClass(a.sensors_norm.oil_quality_score)}${ch("oil")}">${oilLabel(a)}</span></div>
        <div class="sr"><span>Load</span><span class="val ${s.load_factor_current >= 0.85 ? "bad" : s.load_factor_current >= 0.7 ? "warn" : "ok"}${ch("load")}">${Math.round(s.load_factor_current * 100)}%</span></div>
      </div></div>
      <div class="alert-box ${a.status}"><b>${alertTitle(a)}</b>${esc(alertText(a))}</div>
      <div class="impact"><b>Potential Impact</b>
        <span>⌂ ~${fmtInt(gi.customers_served)} customers affected</span>
        <span>◷ Estimated outage: ${outageEst(a)}</span>
        <span>⚠ ${gi.critical_facility_count ? gi.critical_facility_count + " critical facilit" + (gi.critical_facility_count > 1 ? "ies" : "y") + " downstream" : "No critical facilities"} (${esc(a.region)} · ${gi.has_redundant_path ? "N-1 redundant" : "no redundancy"})</span>
      </div>`;
  } else if (S.sideTab === "sensors") {
    body = `<div class="sensor-rows">` + [
      ["Top-oil temp", `${s.top_oil_temp_c} °C`, a.sensors_norm.temperature_score, "alarm 98 °C", ch("temp")],
      ["Hot-spot winding", `${s.winding_hot_spot_c} °C`, a.sensors_norm.temperature_score, "alarm 128 °C", ch("hotspot")],
      ["Vibration", `${s.vibration_mm_s} mm/s`, a.sensors_norm.vibration_score, "alarm 4.5 mm/s", ch("vib")],
      ["Oil dielectric", `${s.oil_dielectric_kv} kV`, a.sensors_norm.oil_quality_score, "new ≥ 70 kV · fail < 30 kV", ch("oil")],
      ["Partial discharge", `${fmtInt(s.partial_discharge_pc)} pC`, a.sensors_norm.partial_discharge_score, "alarm > 1000 pC", ch("pd")],
      ["Current load factor", `${Math.round(s.load_factor_current * 100)}%`, null, "of rated capacity", ch("load")],
    ].map(([k, v, n, h, c]) => `<div class="sr"><span>${k}<br><small style="color:var(--faint)">${h}</small></span>
      <span class="val ${n == null ? "" : riskClass(n)}${c || ""}">${v}${n == null ? "" : `<br><small>score ${n}</small>`}</span></div>`).join("") + `</div>`;
  } else if (S.sideTab === "weather") {
    const n = a.weather_norm;
    body = `<div class="sensor-rows">` + [
      ["Max / min temp", `${w.max_temp_c} / ${w.min_temp_c} °C`, n.temperature_stress_score, ch("wmax") || ch("wmin")],
      ["Precipitation", `${w.precipitation_mm} mm`, n.precipitation_score, ch("precip")],
      ["Max wind", `${w.wind_speed_max_kmh} km/h`, n.wind_storm_score, ch("wind")],
      ["Storm warning", `Level ${w.storm_warning_level} / 3`, null, ""],
      ["Forecast window", `${w.forecast_hours} h`, null, ""],
    ].map(([k, v, sc, c]) => `<div class="sr"><span>${k}</span><span class="val ${sc == null ? "" : riskClass(sc)}${c || ""}">${v}${sc == null ? "" : `<br><small>score ${sc}</small>`}</span></div>`).join("") + `</div>`;
  } else {
    const h = a.history, d = a.degradation, lc = a.lifecycle;
    body = `<div class="sensor-rows">
      <div class="sr"><span>Failures (5 yr)</span><span class="val ${h.failure_count_last_5yr ? "warn" : "ok"}">${h.failure_count_last_5yr}</span></div>
      <div class="sr"><span>Weather-caused</span><span class="val">${h.failures_caused_by_weather}</span></div>
      <div class="sr"><span>Last failure</span><span class="val">${h.last_failure_days_ago == null ? "never" : h.last_failure_days_ago + " days ago"}</span></div>
      <div class="sr"><span>Repeat fault mode</span><span class="val ${h.repeat_mode_flag ? "bad" : "ok"}">${h.repeat_mode_flag ? "YES — unresolved" : "no"}</span></div>
      <div class="sr"><span>Age / rated life</span><span class="val ${lc.past_rated_lifespan ? "bad" : ""}">${d.age_years} / ${lc.rated_lifespan_years} yr</span></div>
      <div class="sr"><span>Lifecycle state</span><span class="val ok">${esc(lc.state)}</span></div>
      <div class="sr"><span>Maintenance overdue</span><span class="val ${d.maintenance_overdue_days > 90 ? "bad" : d.maintenance_overdue_days ? "warn" : "ok"}">${d.maintenance_overdue_days} days</span></div>
      <div class="sr"><span>Insulation health</span><span class="val">${d.insulation_health_pct == null ? "—" : d.insulation_health_pct + "%"}</span></div>
    </div>`;
  }
  el.innerHTML = `
    <div class="sp-head"><span class="sp-id-ico" aria-hidden="true">${TYPE_ICON[a.asset_type] || ICON_TX}</span>
      <span class="aid">${esc(a.id)}</span><span class="status-pill ${a.status}${ch("status")}">◉ ${riskWord(a)}</span></div>
    <div class="sp-sub">${esc(cap(a.asset_type))} &nbsp;|&nbsp; ${esc(a.substation)} · ${esc(a.region)} Zone</div>
    <div class="sp-tabs">${tabs}</div>${body}
    <div class="sp-actions">
      <button class="btn ghost" id="sp-details">View Details</button>
      <button class="btn primary" id="sp-maint">Schedule Maintenance</button>
    </div>`;
  el.querySelectorAll(".sp-tabs button").forEach((b) => b.onclick = () => { S.sideTab = b.dataset.t; renderSide(); });
  $("sp-details").onclick = () => openModal(a.id);
  $("sp-maint").onclick = () => openConfirm(a.id);
}

const riskWord = (a) => a.status === "Healthy" ? "Healthy" : a.status === "Monitoring" ? "Monitoring" : a.status === "High" ? "High Risk" : "Out of Order";
const vibLabel = (a) => a.sensors_raw.vibration_mm_s >= 3 ? "High ↑" : a.sensors_raw.vibration_mm_s >= 1.5 ? "Elevated" : "Normal";
const oilLabel = (a) => a.sensors_raw.oil_dielectric_kv < 30 ? "Degraded ↑" : a.sensors_raw.oil_dielectric_kv < 45 ? "Declining" : "Good";
function alertTitle(a) {
  return a.status === "Critical" ? "⚠ Critical Issue" : a.status === "High" ? "⚠ High Risk" :
    a.status === "Monitoring" ? "◉ Watch — trending up" : "◉ Operating normally";
}
function alertText(a) {
  if (a.status === "Healthy") return `${a.id} is within normal operating bounds. Dominant factor ${a.dominant_factor_label} scores ${a.components[a.dominant_factor]}/100.`;
  // Rank by weighted contribution (score × engine weight), matching the
  // backend's dominant_factor — "likely to fail" and "catastrophic" differ.
  const w = a.component_weights || {};
  const top = Object.entries(a.components).sort((x, y) => (y[1] * (w[y[0]] || 0)) - (x[1] * (w[x[0]] || 0))).slice(0, 2)
    .map(([k, v]) => `${a.component_labels[k]} ${v}/100`).join(" · ");
  return `${a.id}: elevated ${top}. ${a.action_detail}.`;
}
const outageEst = (a) => a.status === "Critical" ? "4–8 hours" : a.status === "High" ? "2–5 hours" : a.status === "Monitoring" ? "1–2 hours" : "—";

/* ---------------- queue + strips ---------------- */
function renderQueue() {
  const ranked = [...filtered()].sort((a, b) => b.overall_risk - a.overall_risk);
  if (!ranked.length) {
    $("queue").innerHTML = `<div class="queue-empty">No assets match the current filters. <button type="button" class="linklike" id="queue-clear">Clear filters</button></div>`;
    $("queue-clear").onclick = clearFilters;
    return;
  }
  $("queue").innerHTML = ranked.map((a, i) => `
    <button type="button" class="qcard ${a.status}" data-id="${a.id}" aria-label="${a.id}, ${a.substation}, ${a.status}, risk ${a.overall_risk} of 100. Activate to inspect.">
      <span class="qr"><span>#${i + 1} priority</span><b style="color:${COLORS[a.status]}">${a.overall_risk}</b></span>
      <span class="qa">${a.id} · ${esc(a.substation)}</span>
      <span class="qd">${a.status} · ${esc(a.dominant_factor_label)} · ${fmtInt(a.grid_impact.customers_served)} customers</span>
    </button>`).join("");
  document.querySelectorAll(".qcard").forEach((c) => c.onclick = () => {
    selectNode(c.dataset.id);
    document.querySelector(".overview-grid").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function renderStrips() {
  const list = filtered();
  if (!list.length) {
    $("weather-card").innerHTML = `<div class="lead"><span class="ico" aria-hidden="true">🌧</span>Upcoming Weather Impact</div>
      <p>No assets in the current filter selection.</p>`;
    $("insights-card").innerHTML = `<div class="lead"><span class="ico" aria-hidden="true">✦</span>AI Insights</div>
      <p>Clear the filters to see fleet insights.</p>`;
    return;
  }
  const byPrecip = [...list].sort((a, b) => b.weather_raw.precipitation_mm - a.weather_raw.precipitation_mm)[0];
  const stormy = list.filter((a) => a.components.weather_risk >= 60);
  const regions = [...new Set(stormy.map((a) => a.region))];
  $("weather-card").innerHTML = `<div class="lead"><span class="ico" aria-hidden="true">🌧</span>Upcoming Weather Impact</div>
    <p>Heavy rainfall expected in <b style="color:var(--text)">${esc(byPrecip.region)} Zone</b>
    (${byPrecip.weather_raw.precipitation_mm} mm, wind ${byPrecip.weather_raw.wind_speed_max_kmh} km/h).
    Increases risk for ${stormy.length} nearby asset${stormy.length === 1 ? "" : "s"}.</p>`;
  const top = [...list].sort((a, b) => b.overall_risk - a.overall_risk)[0];
  $("insights-card").innerHTML = `<div class="lead"><span class="ico" aria-hidden="true">✦</span>AI Insights <span class="go">›</span></div>
    <p>${stormy.length} asset${stormy.length === 1 ? " shows" : "s show"} elevated risk due to forecasted storms.
    ${top.status === "Critical" || top.status === "High"
      ? `Consider pre-positioning maintenance crews in ${esc(top.region)} Zone — ${top.id} is ${top.status.toLowerCase()} at ${top.overall_risk}/100.`
      : "No crew pre-positioning currently required."}</p>`;
}

/* ---------------- assets table ---------------- */
function renderAssetsTable() {
  const rows = [...filtered()].sort((a, b) => b.overall_risk - a.overall_risk);
  if (!rows.length) {
    $("assets-tbody").innerHTML = `<tr><td colspan="9" class="empty-cell">No assets match the current filters. <button type="button" class="linklike" id="assets-clear">Clear filters</button></td></tr>`;
    $("assets-clear").onclick = clearFilters;
    return;
  }
  $("assets-tbody").innerHTML = rows.map((a) => `<tr data-id="${a.id}">
    <td><b>${a.id}</b><br><small style="color:var(--faint)">${esc(cap(a.asset_type))}</small></td>
    <td>${esc(a.substation)}</td><td>${esc(a.region)}</td>
    <td><span class="status-pill ${a.status}">${a.status}</span></td>
    <td><span class="riskbar"><span class="track"><span class="fill" style="width:${a.overall_risk}%;background:${barColor(a.overall_risk)}"></span></span>${a.overall_risk}</span></td>
    <td>${esc(a.dominant_factor_label)}</td>
    <td style="font-family:var(--mono)">${fmtInt(a.grid_impact.customers_served)}</td>
    <td><small>${esc(a.recommended_action)}</small></td>
    <td><button class="linklike" data-open="${a.id}">Details ›</button></td></tr>`).join("");
  bindOpenButtons($("assets-tbody"));
}

/* ---------------- maintenance view ---------------- */
function renderPlan() {
  const p = S.priorities;
  if (!p) {
    $("plan-meta").textContent = "Loading…";
    $("plan-tbody").innerHTML = `<tr><td colspan="9" class="empty-cell">Maintenance plan loading…</td></tr>`;
    $("crew-list").innerHTML = "";
    return;
  }

  const isSimPlan = p.simulation_phase != null;
  const pendingApproval = p.pending_approval === true;

  // Maintenance meta description
  let metaText = `Ranked by risk + grid impact · ${new Date(p.generated_at).toLocaleString()}`;
  if (isSimPlan) metaText += ` · Simulation: ${esc(p.simulation_phase)}`;
  if (p.approved) metaText += ` · ✓ Approved by ${esc(p.approved_by)}`;
  $("plan-meta").textContent = metaText;

  // Simulation plan notice — the "Priority updated by simulation" status.
  // Counts and reasons come from the backend task_changes + change_summary.
  const planNotice = $("sim-plan-notice");
  if (planNotice) {
    if (isSimPlan && pendingApproval) {
      planNotice.hidden = false;
      const deltas = p.task_changes || [];
      const nUp = deltas.filter((c) => c.moved_up).length;
      const nEsc = deltas.filter((c) => c.newly_high_critical).length;
      let head = `⚠ <b>Priority updated by simulation</b> — ${esc(p.simulation_phase)}. `;
      head += deltas.length
        ? `${deltas.length} item${deltas.length === 1 ? "" : "s"} changed` +
          `${nUp ? ` (${nUp} promoted)` : ""}${nEsc ? `, ${nEsc} newly high/critical` : ""}.`
        : "Priorities reordered from live engine scores.";
      const sched = (p.change_summary || []).length
        ? `<br><small>Scheduler: ${p.change_summary.map(esc).join("; ")}.</small>` : "";
      planNotice.innerHTML = head + sched;
    } else if (isSimPlan && p.approved) {
      planNotice.hidden = false;
      planNotice.innerHTML = `✓ <b>Simulation plan approved</b> (${esc(p.approved_by)} · ${new Date(p.approved_at).toLocaleString()})`;
    } else {
      planNotice.hidden = true;
    }
  }

  // FLIP preparation: record row positions BEFORE the re-render so rank
  // reordering can be animated (rows that moved slide to their new slot).
  const tbody = $("plan-tbody");
  const prevTops = new Map();
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => prevTops.set(tr.dataset.id, tr.getBoundingClientRect().top));

  const deltaById = new Map((p.task_changes || []).map((c) => [c.asset_id, c]));
  tbody.innerHTML = (p.maintenance_plan || []).map((r) => {
    const ch = deltaById.get(r.asset_id);
    const reordered = r.reordered === true;
    // Row highlight priority: new escalation > status change > rank move.
    let rowClass = "";
    if (ch && ch.newly_high_critical) rowClass = "plan-row-escalated";
    else if (ch && ch.status_from !== ch.status_to) rowClass = "plan-row-status";
    else if (ch && ch.moved_up) rowClass = "plan-row-moved-up";
    else if (ch && ch.moved_down) rowClass = "plan-row-moved-down";
    else if (reordered) rowClass = "plan-row-reordered";
    const rankBadge = (ch && ch.rank_from !== ch.rank_to)
      ? ` <span class="rank-delta ${ch.moved_up ? "up" : "down"}" title="Was #${ch.rank_from} before the simulation update">${ch.moved_up ? "▲" : "▼"}${Math.abs(ch.rank_delta)}</span>`
      : "";
    const crewChanged = ch && (ch.crew_from || null) !== (ch.crew_to || null);
    const crew = r.assigned_crew
      ? `<span class="plan-crew-tag${crewChanged ? " crew-changed" : ""}"${crewChanged ? ` title="Reassigned by scheduler (was ${esc(ch.crew_from || "unassigned")})"` : ""}>${esc(r.assigned_crew)}</span>`
      : `<span style="color:var(--faint)">—</span>`;
    // Scheduler urgency (present on simulation-plan rows); highlighted when
    // the backend scheduler changed it.
    const urgChanged = ch && ch.urgency_from !== ch.urgency_to;
    const urg = r.urgency
      ? `<br><span class="urgency ${esc(r.urgency)}${urgChanged ? " urg-changed" : ""}"${urgChanged ? ` title="Urgency changed by scheduler (was ${esc(ch.urgency_from)})"` : ` title="Scheduler urgency"`}>${esc(r.urgency)}</span>`
      : "";
    // "Priority updated by simulation" reason line with scheduler factors.
    const reacted = ch && (ch.moved_up || ch.moved_down || ch.status_from !== ch.status_to || urgChanged || crewChanged);
    const reason = (reacted && ch.scheduling_reason)
      ? `<span class="plan-reason">⚡ Priority updated by simulation — ${esc(ch.scheduling_reason)}</span>`
      : "";
    return `<tr data-id="${r.asset_id}"${rowClass ? ` class="${rowClass}"` : ""}>
      <td><b>#${r.rank}</b>${rankBadge}</td>
      <td><b>${r.asset_id}</b><br><small style="color:var(--faint)">${esc(r.substation)}</small></td>
      <td><span class="status-pill ${r.status}">${r.status}</span></td>
      <td style="font-family:var(--mono);font-weight:700">${r.overall_risk}</td>
      <td style="font-family:var(--mono)">${fmtInt(r.customers_served)}</td>
      <td>${r.has_redundant_path ? "N-1 ✓" : "<b style='color:var(--red)'>none</b>"}</td>
      <td><b>${esc(r.recommended_action)}</b><br><small style="color:var(--muted)">${esc(r.action_detail)}</small>${urg}${reason}</td>
      <td>${crew}</td>
      <td><button class="linklike" data-open="${r.asset_id}">Details ›</button></td>
    </tr>`;
  }).join("");
  bindOpenButtons(tbody);
  // FLIP: slide rows that changed vertical position to their new slot.
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    const old = prevTops.get(tr.dataset.id);
    if (old == null) return;
    const dy = old - tr.getBoundingClientRect().top;
    if (Math.abs(dy) < 4) return;
    tr.style.transform = `translateY(${dy}px)`;
    tr.style.transition = "none";
    requestAnimationFrame(() => requestAnimationFrame(() => {
      tr.style.transition = "transform .5s cubic-bezier(.2,.7,.25,1)";
      tr.style.transform = "";
      setTimeout(() => { tr.style.transition = ""; tr.style.transform = ""; }, 550);
    }));
  });
  $("crew-list").innerHTML = (p.crew_prepositioning || []).length ? (p.crew_prepositioning || []).map((c) => `
    <div class="panel crew-card"><h3>${SVG_CREW} ${esc(c.region)} Zone staging</h3><p>${esc(c.reason)}</p>
    <div class="chips">${c.assets.map((id) => {
      const a = S.assets.find((x) => x.id === id);
      if (!a) return `<span class="chip">${esc(id)}</span>`;
      return `<span class="chip ${a.status}">${id} · ${a.overall_risk}</span>`;
    }).join("")}</div></div>`).join("")
    : `<div class="panel crew-card"><h3>${SVG_CREW} No staging required</h3><p>No weather-exposed, high-consequence assets right now.</p></div>`;
}
function bindOpenButtons(root) {
  root.querySelectorAll("[data-open]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); openModal(b.dataset.open); });
  root.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => openModal(tr.dataset.id));
}

/* ---------------- modal ---------------- */
/* Asset detail body — a pure render of backend state, re-runnable so an open
   record stays in sync with live simulation scores without stealing focus. */
function renderModalBody(id) {
  const a = S.assets.find((x) => x.id === id);
  if (!a) return false;
  const lc = a.lifecycle, h = a.history, d = a.degradation, gi = a.grid_impact, s = a.sensors_raw;
  // Order bars by weighted contribution (score × engine weight) so the
  // top bar always matches the backend's dominant_factor.
  const _w = a.component_weights || {};
  const fbars = Object.entries(a.components).sort((x, y) => (y[1] * (_w[y[0]] || 0)) - (x[1] * (_w[x[0]] || 0))).map(([k, v]) => `
    <div class="fbar"><div class="fl"><span>${esc(a.component_labels[k])} <small style="color:var(--faint)">× ${a.component_weights[k].toFixed(2)}</small></span>
    <b style="color:${barColor(v)}">${v}/100</b></div>
    <div class="track"><div class="fill" style="width:${v}%;background:${barColor(v)}"></div></div></div>`).join("");
  const kv = (rows) => `<dl class="kv">` + rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") + `</dl>`;
  const mh = lc.maintenance_history.length ? lc.maintenance_history.map((m) => `<span class="tag">maint · ${m.days_ago}d ago · ${esc(m.notes || "logged")}</span>`).join("") : `<span class="tag">no logged interventions — state reflects current readings</span>`;
  const fh = lc.fault_history.length ? lc.fault_history.map((f) => `<span class="tag">fault · ${esc(f.severity)} · ${f.days_ago}d ago</span>`).join("") : `<span class="tag">no discrete fault events logged</span>`;
  const lineage = a.lineage.predecessor || a.lineage.successor_of_retired
    ? `<p style="font-size:12.5px;margin:6px 0 0">⛓ ${a.lineage.predecessor ? `Replaces retired <b>${esc(a.lineage.predecessor.asset_id)}</b> (${esc(a.lineage.predecessor.retirement_reason || "replaced")}).` : ""}
       ${a.lineage.successor_of_retired ? `Successor of retired <b>${esc(a.lineage.successor_of_retired.asset_id)}</b>.` : ""}</p>`
    : `<p style="font-size:12.5px;color:var(--muted);margin:6px 0 0">Original unit — no replacement lineage.</p>`;
  $("modal-body").innerHTML = `
    <div class="m-head"><span class="m-id-ico" aria-hidden="true">${TYPE_ICON[a.asset_type] || ICON_TX}</span>
      <h2>${a.id}</h2><span class="status-pill ${a.status}">${riskWord(a)} · ${a.overall_risk}/100</span></div>
    <div class="m-sub">${esc(cap(a.asset_type))} · ${esc(a.substation)} · ${esc(a.region)} Zone · commissioned ${a.commissioned_year} · ${(a.rated_kva / 1000).toFixed(0)} MVA · ${a.rated_voltage_kv} kV</div>
    <div class="m-grid">
      <div class="m-card full"><h4>Why ${a.id} is ${a.status} — risk breakdown (weights sum to 1.0)</h4>${fbars}
        <div class="why-box">Primary driver: <b>${esc(a.dominant_factor_label)}</b>. ${esc(alertText(a))}</div></div>
      <div class="m-card"><h4>Grid impact</h4>${kv([
        ["Customers", fmtInt(gi.customers_served)], ["Critical facilities", gi.critical_facility_count],
        ["Peak load", `${gi.peak_load_mw} MW`], ["Downstream assets", gi.downstream_asset_count],
        ["Redundancy", gi.has_redundant_path ? "N-1 ✓" : "NONE"],
      ])}${gi.critical_facility_names.length ? `<p style="font-size:12px;color:var(--muted)">▣ ${gi.critical_facility_names.map(esc).join(" · ")}</p>` : ""}</div>
      <div class="m-card"><h4>Failure history</h4>${kv([
        ["Failures (5 yr)", h.failure_count_last_5yr], ["Weather-caused", h.failures_caused_by_weather],
        ["Last failure", h.last_failure_days_ago == null ? "never" : h.last_failure_days_ago + " days ago"],
        ["Repeat mode", h.repeat_mode_flag ? "YES" : "no"],
        ["MTBF", h.mean_time_between_failures_days == null ? "—" : h.mean_time_between_failures_days + " days"],
      ])}</div>
      <div class="m-card"><h4>Degradation & lifecycle</h4>${kv([
        ["State", esc(lc.state)], ["Age", `${d.age_years} yr (rated ${lc.rated_lifespan_years})`],
        ["Remaining life", `${lc.remaining_life_years} yr`], ["Insulation", d.insulation_health_pct == null ? "—" : d.insulation_health_pct + "%"],
        ["Fault events", d.cumulative_fault_events], ["Maint. overdue", `${d.maintenance_overdue_days} days`],
      ])}</div>
      <details class="m-card"><summary><span>Sensor evidence</span><span class="sum-val">Hot-spot ${s.winding_hot_spot_c} °C</span></summary>${kv([
        ["Top-oil temp", `${s.top_oil_temp_c} °C`], ["Hot-spot", `${s.winding_hot_spot_c} °C`],
        ["Vibration", `${s.vibration_mm_s} mm/s`], ["Oil dielectric", `${s.oil_dielectric_kv} kV`],
        ["Partial discharge", `${fmtInt(s.partial_discharge_pc)} pC`], ["Load now", `${Math.round(s.load_factor_current * 100)}%`],
      ])}</details>
      <details class="m-card"><summary><span>Weather exposure (72 h)</span><span class="sum-val">Storm ${a.weather_raw.storm_warning_level}/3 · ${a.weather_raw.precipitation_mm} mm</span></summary>${kv([
        ["Max / min", `${a.weather_raw.max_temp_c} / ${a.weather_raw.min_temp_c} °C`],
        ["Precipitation", `${a.weather_raw.precipitation_mm} mm`], ["Max wind", `${a.weather_raw.wind_speed_max_kmh} km/h`],
        ["Storm level", `${a.weather_raw.storm_warning_level} / 3`],
        ["Weather score", `${a.components.weather_risk}/100`],
      ])}</details>
      <div class="m-card"><h4>Maintenance & fault log</h4><div>${mh}</div><div style="margin-top:6px">${fh}</div>
        ${lineage}</div>
    </div>
    <div class="m-actions">
      <button class="btn primary" id="m-ask">✦ Ask AI about ${a.id}</button>
      <button class="btn ghost" id="m-maint">Schedule Maintenance</button>
    </div>`;
  $("m-ask").onclick = () => { closeModal(); gotoAI(`Why is ${a.id} ${a.status.toLowerCase()}?`, a.id); };
  $("m-maint").onclick = () => openConfirm(a.id);
  return true;
}
/* Open the asset modal (records which asset is open so live simulation
   refreshes can keep its numbers in sync). */
function openModal(id) {
  if (!renderModalBody(id)) return;
  S._modalAsset = id;
  $("modal-backdrop").classList.remove("hidden");
  S._lastFocus = document.activeElement;
  const closeBtn = $("modal-close");
  if (closeBtn) closeBtn.focus();
}
function trapTab(e, container) {
  if (e.key !== "Tab" || !container) return;
  const f = container.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  const vis = [...f].filter((el) => !el.disabled && el.offsetParent !== null);
  if (!vis.length) return;
  const first = vis[0], last = vis[vis.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
function closeModal() {
  $("modal-backdrop").classList.add("hidden");
  S._modalAsset = null;
  if (S._lastFocus && S._lastFocus.focus) { try { S._lastFocus.focus(); } catch (e) {} S._lastFocus = null; }
}

/* Confirm step before drafting a work order: shows the three facts that
   matter (status/risk, customers, redundancy) with a cancel path. */
function openConfirm(id) {
  const a = S.assets.find((x) => x.id === id);
  if (!a) return;
  const gi = a.grid_impact;
  $("confirm-body").innerHTML = `
    <h2 id="confirm-title">Schedule maintenance</h2>
    <div class="m-sub">${esc(a.id)} · ${esc(a.substation)} · ${esc(a.region)} Zone</div>
    <div class="confirm-facts">
      <div class="sr"><span>Status</span><span class="val">${a.status} · ${a.overall_risk}/100</span></div>
      <div class="sr"><span>Customers affected</span><span class="val">~${fmtInt(gi.customers_served)}</span></div>
      <div class="sr"><span>Redundancy</span><span class="val">${gi.has_redundant_path ? "N-1 redundant" : "NONE"}</span></div>
      <div class="sr"><span>Action</span><span class="val">${esc(a.recommended_action)}</span></div>
    </div>
    <p class="confirm-note">Demo build — drafting only, nothing is dispatched.</p>
    <div class="confirm-actions">
      <button type="button" class="btn ghost" id="confirm-cancel">Cancel</button>
      <button type="button" class="btn primary" id="confirm-ok">Draft work order</button>
    </div>`;
  $("confirm-backdrop").classList.remove("hidden");
  S._lastConfirmFocus = document.activeElement;
  $("confirm-cancel").onclick = closeConfirm;
  $("confirm-ok").onclick = () => {
    closeConfirm();
    toast(`Work order drafted for ${a.id} — ${a.recommended_action} (demo)`);
  };
  $("confirm-cancel").focus();
}
function closeConfirm() {
  $("confirm-backdrop").classList.add("hidden");
  if (S._lastConfirmFocus && S._lastConfirmFocus.focus) { try { S._lastConfirmFocus.focus(); } catch (e) {} S._lastConfirmFocus = null; }
}

function openHelp(kind) {
  const guides = {
    overview: {
      eyebrow: "GRID STATUS",
      title: "Read the network at a glance",
      lead: "Use this view to spot risk, inspect an asset, and understand where operational attention is needed.",
      steps: [["Filter the fleet", "Search by asset ID, status, region, or asset type. The map and attention queue update together."], ["Read the health scale", "Green means healthy, yellow means monitoring, orange means high risk, and red means critical."], ["Watch live changes", "During a simulation, a flashing ring marks an asset whose risk just changed, a halo marks one that keeps deteriorating, and the banner feed lists every transition."], ["Inspect a node", "Activate a transformer or substation (Enter) for live details. Double-click it for the full asset record."], ["Navigate the map", "Use +, minus, Reset, or your mouse wheel to zoom into the network."], ["Move to action", "Use the Maintenance view when an asset needs a ranked response plan."]]
    },
    assets: {
      eyebrow: "ASSET REGISTER",
      title: "Inspect the asset register",
      lead: "Use Assets when you need a precise, sortable view of every transformer and substation in the fleet.",
      steps: [["Scan the table", "Compare status, risk score, dominant factor, customer impact, and recommended action in one row."], ["Open details", "Select any row or choose Details to view sensors, weather, history, lifecycle, and grid impact."], ["Follow the risk", "The bar and score show relative exposure; the status color shows the operational urgency."], ["Return to context", "Use the asset record actions to jump into Guard AI or schedule a maintenance response."]]
    },
    maintenance: {
      eyebrow: "MAINTENANCE CONTROL",
      title: "Turn risk into a response plan",
      lead: "Use Maintenance to prioritize work by risk and grid consequence, then prepare crews for exposed regions.",
      steps: [["Start at rank one", "The plan is ordered by overall risk with grid impact as the tie-break, so the highest-consequence work appears first."], ["Read the action", "The bold recommendation is the proposed intervention; the supporting line explains the operational reason."], ["Check redundancy", "N-1 indicates a redundant path. None means an outage carries higher consequence."], ["Open asset details", "Choose Details for the full evidence record behind a recommendation."], ["Review crews", "Crew Pre-positioning highlights weather-exposed, high-consequence assets by region."]]
    },
    ai: {
      eyebrow: "GUARD AI",
      title: "Ask grounded operational questions",
      lead: "Guard AI explains the scores already shown in GridGuard so you can investigate without losing the source context.",
      steps: [["Choose context", "Select an asset or leave the selector on Fleet-wide for a broader answer."], ["Use a suggestion", "Start with a suggested question, or ask why an asset is high risk and what to inspect."], ["Check the evidence", "Answers include the asset IDs and scores used to ground the response."], ["Act on the result", "Use the answer alongside the Maintenance plan and asset detail record before assigning work."]]
    }
  };
  const guide = guides[S.currentView] || guides.overview;
  const content = kind === "contact" ? `
    <div class="help-eyebrow">GRIDGUARD SUPPORT</div>
    <h2 id="help-title">Contact the control room</h2>
    <p class="help-lead">For access, data, or operational questions, contact your GridGuard administrator or the platform support desk.</p>
    <div class="help-contact"><b>Platform support</b><span>support@gridguard.local</span><small>Include the asset ID and time of the issue when reporting a problem.</small></div>` : `
    <div class="help-eyebrow">${guide.eyebrow}</div>
    <h2 id="help-title">${guide.title}</h2>
    <p class="help-lead">${guide.lead}</p>
    <ol class="help-steps">${guide.steps.map(([title, text]) => `<li><b>${title}</b><span>${text}</span></li>`).join("")}</ol>`;
  $("help-body").innerHTML = content;
  $("help-backdrop").classList.remove("hidden");
  S._lastHelpFocus = document.activeElement;
  const hc = $("help-close");
  if (hc) hc.focus();
}
function closeHelp() {
  $("help-backdrop").classList.add("hidden");
  if (S._lastHelpFocus && S._lastHelpFocus.focus) { try { S._lastHelpFocus.focus(); } catch (e) {} S._lastHelpFocus = null; }
}

/* ---------------- AI ---------------- */
const SUGGESTIONS = [
  "Which assets should we inspect today?",
  "Why is TX-007 critical?",
  "What factors are driving TX-008's risk?",
  "Should we inspect TX-005 this week?",
];

/* Safe lightweight markdown renderer (no dependencies, XSS-safe).
   Escapes HTML first, then emits only our own allowlisted tags. */
function renderMarkdown(src) {
  const text = String(src == null ? "" : src);
  // 1. Extract fenced code blocks so inner markdown is not processed.
  const codeBlocks = [];
  let work = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    codeBlocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000CODE${codeBlocks.length - 1}\u0000`;
  });
  const lines = work.split("\n");
  let html = "";
  let inUL = false, inOL = false;
  const closeLists = () => {
    if (inUL) { html += "</ul>"; inUL = false; }
    if (inOL) { html += "</ol>"; inOL = false; }
  };
  const inline = (s) => {
    let o = esc(s);
    // inline code
    o = o.replace(/`([^`]+)`/g, "<code>$1</code>");
    // bold + italic
    o = o.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    o = o.replace(/(^|\W)\*([^*\n]+)\*/g, "$1<em>$2</em>");
    // asset IDs become detail buttons (TX-001 style)
    o = o.replace(/\bTX-?(\d{3})\b/g, (m) => {
      const id = m.replace("TX", "TX-").replace("TX--", "TX-").toUpperCase();
      const norm = /^TX-\d{3}$/.test(id) ? id : m.toUpperCase();
      return `<button type="button" class="asset-link" data-open-asset="${esc(norm)}">${esc(m)}</button>`;
    });
    return o;
  };
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const t = line.trim();
    if (!t) { closeLists(); continue; }
    if (t.includes("\u0000CODE")) { closeLists(); html += t; continue; }
    // markdown table
    if (isTableRow(line)) {
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) { rows.push(lines[i]); i++; }
      i--;
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const isSep = (r) => /^[\s|:|-]+$/.test(r) && r.includes("-");
      let start = 0, header = null;
      if (rows.length > 1 && isSep(rows[1])) { header = cells(rows[0]); start = 2; }
      html += `<div class="md-table-wrap"><table class="md-table">`;
      if (header) html += `<thead><tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`;
      html += "<tbody>";
      for (let k = start; k < rows.length; k++) {
        if (isSep(rows[k])) continue;
        html += `<tr>${cells(rows[k]).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`;
      }
      html += "</tbody></table></div>";
      closeLists();
      continue;
    }
    // headings
    const h = t.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      closeLists();
      const lvl = Math.min(4, h[1].length);
      html += `<h${lvl + 1} class="md-h">${inline(h[2])}</h${lvl + 1}>`;
      continue;
    }
    // horizontal rule
    if (/^(-{3,}|\*{3,})$/.test(t)) { closeLists(); html += "<hr class='md-hr'>"; continue; }
    // blockquote
    if (/^&gt;/.test(esc(t)) || /^>/.test(t)) {
      closeLists();
      html += `<blockquote class="md-quote">${inline(t.replace(/^>\s?/, ""))}</blockquote>`;
      continue;
    }
    // unordered list
    let m = t.match(/^([-*•])\s+(.*)/);
    if (m) {
      if (inOL) { html += "</ol>"; inOL = false; }
      if (!inUL) { html += `<ul class="md-ul">`; inUL = true; }
      html += `<li>${inline(m[2])}</li>`;
      continue;
    }
    // ordered list
    m = t.match(/^(\d+)[.)]\s+(.*)/);
    if (m) {
      if (inUL) { html += "</ul>"; inUL = false; }
      if (!inOL) { html += `<ol class="md-ol">`; inOL = true; }
      html += `<li>${inline(m[2])}</li>`;
      continue;
    }
    closeLists();
    html += `<p class="md-p">${inline(t)}</p>`;
  }
  closeLists();
  // restore code blocks
  html = html.replace(/\u0000CODE(\d+)\u0000/g, (mm, n) => codeBlocks[Number(n)] || "");
  return html || `<p class="md-p">—</p>`;
}

function timeNow() {
  return new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function renderAIContext() {
  const sel = $("ai-asset");
  const id = sel ? sel.value : "";
  const a = S.assets.find((x) => x.id === id);
  const list = a ? [a] : [...S.assets].sort((x, y) => y.overall_risk - x.overall_risk).slice(0, 4);
  const card = (x) => {
    const sub = S.assets.find((y) => y.id === x.id) || x;
    return `<div class="ctx-card">
      <div class="ctx-top"><button type="button" class="asset-link strong" data-open-asset="${esc(x.id)}">${esc(x.id)}</button>
        <span class="status-pill ${x.status}">${x.status}</span>
        <span class="ctx-risk" style="color:${barColor(x.overall_risk)}">${x.overall_risk}</span></div>
      <span class="bar"><i style="width:${x.overall_risk}%;background:${barColor(x.overall_risk)}"></i></span>
      <small>${esc(x.dominant_factor_label)} ${x.components[x.dominant_factor]}/100 · ${fmtInt(x.grid_impact.customers_served)} customers · ${esc(sub.substation || "")}</small>
    </div>`;
  };
  $("ai-context").innerHTML =
    `<div class="ctx-head"><h3>${a ? esc(a.id) + " — live scores" : "Fleet context — live scores"}</h3>
     <span class="ctx-live"><i></i>live engine</span></div>` +
    list.map(card).join("") +
    `<p class="ctx-note">Answers are generated from these exact scores via the backend briefing service. Select an asset to scope questions.</p>`;
  bindAssetLinks($("ai-context"));
  const badge = $("chat-ctx-badge");
  if (badge) badge.textContent = a ? `${a.id} · ${a.status} · ${a.overall_risk}/100` : `Fleet-wide · top ${list.length} by risk`;
}

function bindAssetLinks(root) {
  if (!root) return;
  root.querySelectorAll("[data-open-asset]").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); openModal(b.dataset.openAsset); };
  });
}

function bootChat() {
  if (S.chatBooted) return;
  S.chatBooted = true;
  $("suggestions").innerHTML = SUGGESTIONS.map((s) => `<button class="sug" type="button">${esc(s)}</button>`).join("");
  document.querySelectorAll(".sug").forEach((b) => b.onclick = () => ask(b.textContent, $("ai-asset").value || null));
  aiSay("**Guard AI is online.** I explain live risk-engine results — no invented numbers.\n\n- Pick an **asset context** on the right, or stay **Fleet-wide**\n- Ask e.g. `Why is TX-007 critical?`\n- Every answer cites the engine scores it used", [], {}, { welcome: true });
  const clear = $("chat-clear");
  if (clear) clear.onclick = clearChat;
}

function aiSay(text, assetIds, risks, opts) {
  opts = opts || {};
  const chips = (assetIds || []).map((id) =>
    `<button type="button" class="score-chip clickable" data-open-asset="${esc(id)}" title="Open ${esc(id)} details">${esc(id)}${risks && risks[id] != null ? " · " + risks[id] : ""}</button>`).join("");
  const div = document.createElement("div");
  div.className = "msg ai" + (opts.error ? " msg-error" : "") + (opts.welcome ? " msg-welcome" : "");
  const provider = (S.briefing && S.briefing.provider) || "mock";
  const body = opts.plain ? `<p class="md-p">${esc(text)}</p>` : renderMarkdown(text);
  div.innerHTML = `
    <div class="msg-head"><span class="msg-who">Guard AI</span>
      <span class="msg-time">${timeNow()}</span>
      <button type="button" class="copy-btn" title="Copy answer">Copy</button></div>
    <div class="msg-body">${body}</div>
    <div class="meta">${chips}<span class="score-chip static" title="All figures come from the deterministic risk engine">grounded · ${esc(provider)}</span></div>`;
  const copy = div.querySelector(".copy-btn");
  if (copy) copy.onclick = async () => {
    try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; }
    catch (e) { copy.textContent = "Copy failed"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 1400);
  };
  bindAssetLinks(div);
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

function userSay(text) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="msg-head"><span class="msg-who">You</span><span class="msg-time">${timeNow()}</span></div>
    <div class="msg-body"><p class="md-p">${esc(text)}</p></div>`;
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
}

function clearChat() {
  S.history = [];
  $("chat").innerHTML = "";
  aiSay("Conversation cleared. Ask a follow-up — I keep turn-by-turn context until you clear again.", [], {});
  toast("Guard AI conversation cleared");
}

async function ask(question, assetId) {
  const q = String(question == null ? "" : question).trim();
  if (S.asking || !q) return;
  S.asking = true;
  const input = $("chat-input");
  const btn = document.querySelector("#chat-form button[type=submit]");
  if (input) input.disabled = true;
  if (btn) btn.disabled = true;
  // context divider when the operator scoped to a new asset mid-thread
  const lastCtx = S._lastCtx || "";
  if ((assetId || "") !== lastCtx && S.history.length) {
    const d = document.createElement("div");
    d.className = "ctx-divider";
    d.textContent = assetId ? `Context → ${assetId}` : "Context → Fleet-wide";
    $("chat").appendChild(d);
  }
  S._lastCtx = assetId || "";
  userSay(q);
  const t = document.createElement("div");
  t.className = "msg ai msg-typing";
  t.setAttribute("role", "status");
  t.innerHTML = `<div class="msg-head"><span class="msg-who">Guard AI</span></div>
    <div class="typing-row"><span></span><span></span><span></span><em>Consulting risk engine…</em></div>`;
  $("chat").appendChild(t);
  $("chat").scrollTop = $("chat").scrollHeight;
  try {
    const r = await api("/api/briefing", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, asset_id: assetId, history: S.history.slice(-12) }),
    });
    t.remove();
    aiSay(r.text, r.asset_ids, r.overall_risks);
    S.history.push({ role: "user", content: q }, { role: "assistant", content: r.text });
    if (S.history.length > 24) S.history = S.history.slice(-24);
    if (r.notice && !S._noticeShown) {
      S._noticeShown = true;
      toast(r.notice);
    }
  } catch (e) {
    t.remove();
    aiSay("The briefing service is unreachable. Check the backend (`python3 run_ui.py`) and try again.", [], {}, { error: true });
  }
  S.asking = false;
  if (input) { input.disabled = false; input.focus(); }
  if (btn) btn.disabled = false;
}
function gotoAI(prefill, assetId) {
  switchView("ai");
  if (assetId) { $("ai-asset").value = assetId; renderAIContext(); }
  ask(prefill, assetId || null);
}

/* ---------------- chrome ---------------- */
function switchView(name) {
  S.currentView = name;
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.view === name;
    t.classList.toggle("active", on);
    if (on) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "ai") { bootChat(); renderAIContext(); }
}
function badge() {
  const b = $("provider-badge");
  if (S.briefing.offline) { b.textContent = `AI: mock (offline) · grounded`; }
  else { b.textContent = `AI: ${S.briefing.provider} · ${S.briefing.model}`; b.classList.add("live"); }
  const tip = [];
  if (S.briefing.warning) tip.push(S.briefing.warning);
  if (S.briefing.env_files && S.briefing.env_files.length) tip.push("env: " + S.briefing.env_files.join(", "));
  if (tip.length) b.title = tip.join("\n");
}
function startClock() {
  const tick = () => { $("live-clock").textContent = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
  tick(); setInterval(tick, 1000);
}
function renderAll() {
  // Inject nav SVG icons into the static HTML tab spans
  const _ti = { overview: NAV_ICON_OVERVIEW, assets: NAV_ICON_ASSETS, maintenance: NAV_ICON_MAINT, ai: NAV_ICON_AI };
  Object.entries(_ti).forEach(([k, svg]) => { const el = document.getElementById("ti-" + k); if (el) el.innerHTML = svg; });
  renderKPIs(); renderMap(); renderSide(); renderQueue(); renderStrips();
  renderAssetsTable(); renderPlan(); renderFilterMeta();
  if (S.briefing && S.briefing.offline) $("offline-banner").hidden = false;
  $("offline-dismiss").onclick = () => { $("offline-banner").hidden = true; };
  document.querySelectorAll(".tab").forEach((t) => t.onclick = () => switchView(t.dataset.view));
  $("brand-home").onclick = () => switchView("overview");
  $("q").oninput = (e) => { S.filters.q = e.target.value.toLowerCase(); refreshFiltered(); };
  $("f-status").onchange = (e) => { S.filters.status = e.target.value; refreshFiltered(); };
  $("f-region").onchange = (e) => { S.filters.region = e.target.value; refreshFiltered(); };
  $("f-type").onchange = (e) => { S.filters.type = e.target.value; refreshFiltered(); };
  $("map-zoom-in").onclick = () => setMapZoom(S.mapZoom + 0.1);
  $("map-zoom-out").onclick = () => setMapZoom(S.mapZoom - 0.1);
  $("map-zoom-reset").onclick = () => setMapZoom(1);
  $("netmap").onwheel = (e) => {
    e.preventDefault();
    setMapZoom(S.mapZoom + (e.deltaY < 0 ? 0.1 : -0.1));
  };
  $("qa-plan").onclick = () => switchView("maintenance");
  $("qa-crew").onclick = async () => {
    switchView("maintenance");
    document.getElementById("crew-list").scrollIntoView({ behavior: "smooth" });
  };
  $("regen-plan").onclick = async () => {
    S.priorities = await api("/api/priorities"); renderPlan(); toast("Maintenance plan regenerated from live scores");
  };
  $("ai-asset").onchange = renderAIContext;
  $("chat-form").onsubmit = (e) => {
    e.preventDefault();
    const q = $("chat-input").value;
    $("chat-input").value = "";
    ask(q, $("ai-asset").value || null);
  };
  $("modal-close").onclick = closeModal;
  $("modal-backdrop").onclick = (e) => { if (e.target.id === "modal-backdrop") closeModal(); };
  $("modal-backdrop").addEventListener("keydown", (e) => trapTab(e, document.querySelector("#modal-backdrop .modal")));
  $("help-backdrop").addEventListener("keydown", (e) => trapTab(e, document.querySelector("#help-backdrop .modal")));
  $("confirm-backdrop").onclick = (e) => { if (e.target.id === "confirm-backdrop") closeConfirm(); };
  $("confirm-backdrop").addEventListener("keydown", (e) => trapTab(e, document.querySelector("#confirm-backdrop .modal")));
  $("help-trigger").onclick = () => {
    const menu = $("help-dropdown");
    menu.hidden = !menu.hidden;
    $("help-trigger").setAttribute("aria-expanded", String(!menu.hidden));
  };
  document.querySelectorAll("[data-help]").forEach((b) => b.onclick = () => {
    $("help-dropdown").hidden = true;
    $("help-trigger").setAttribute("aria-expanded", "false");
    openHelp(b.dataset.help);
  });
  $("help-close").onclick = closeHelp;
  $("help-backdrop").onclick = (e) => { if (e.target.id === "help-backdrop") closeHelp(); };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".help-menu")) { $("help-dropdown").hidden = true; $("help-trigger").setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeHelp(); closeConfirm(); } });
}

/* ============================================================
   SIMULATION LAYER — SCENARIO RUNNER
   Autonomous timed simulation.  The backend drives all scoring
   and runs the ScenarioRunner; this JS polls /api/scenario/tick
   every 2.5 seconds while running and reflects the state.
   ============================================================ */

const SIM = {
  active: false,
  scenarioActive: false,
  scenarioId: null,
  scenarioLabel: null,
  phase: null,
  phaseIndex: null,
  phaseLabel: null,
  phaseDesc: null,
  totalPhases: 0,
  stepIndex: null,
  totalSteps: null,
  stepProgress: 0,
  stepElapsedS: 0,
  stepDurationS: 0,
  elapsedS: 0,
  running: false,
  paused: false,
  completed: false,
  planPending: false,
  planApproved: false,
  liveWeather: false,
  _pollTimer: null,
  // Live-change visualisation (all derived from backend `changes` events):
  // flash maps asset_id -> one-shot marker class consumed by renderMap();
  // events is the recent-event feed rendered into the banner.
  flash: {},
  events: [],
};

// All available scenarios loaded from the backend
const SIM_SCENARIOS = { list: [], default: "storm_surge" };

async function simApi(path, body) {
  const opts = body != null
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { method: "POST" };
  try {
    const r = await fetch(path, opts);
    if (!r.ok) return null;
    return r.json();
  } catch (e) { return null; }
}

// ── Live-change visualisation ───────────────────────────────────────────────
// Backend `changes` events (per-asset transitions between scoring snapshots)
// drive three UI reactions: a one-shot marker flash on the map, an operator
// event feed in the banner, and a toast for newly elevated priorities.
// Nothing here invents data — it only visualises the backend diff.

function queueSimFlashes(changes) {
  SIM.flash = SIM.flash || {};
  (changes || []).forEach((ch) => {
    if (!ch || !ch.asset_id) return;
    let cls = null;
    if (ch.newly_high_critical) {
      cls = "flash-escalated";
    } else if (ch.cleared_alert || (ch.band_change && ch.direction === "falling")) {
      cls = "flash-improved";
    } else if (ch.band_change || Math.abs(ch.risk_delta || 0) >= 0.5) {
      cls = "flash-changed";
    }
    if (!cls) return;
    // Escalation wins if several transitions queued for the same marker.
    if (SIM.flash[ch.asset_id] !== "flash-escalated") SIM.flash[ch.asset_id] = cls;
  });
}

function pushSimEvents(changes) {
  SIM.events = SIM.events || [];
  (changes || []).forEach((ch) => {
    if (!ch || !ch.asset_id) return;
    // Latest-wins per asset: a new transition updates that asset's row in
    // place instead of appending a duplicate, so the feed stays readable
    // when several ticks in a row move the same assets.
    SIM.events = SIM.events.filter((e) => e.id !== ch.asset_id);
    SIM.events.push({
      id: ch.asset_id,
      from: ch.previous_status,
      to: ch.new_status,
      oldRisk: ch.previous_risk,
      newRisk: ch.new_risk,
      delta: ch.risk_delta,
      dir: ch.direction,
      esc: ch.newly_high_critical,
      improved: ch.cleared_alert,
      band: ch.band_change,
      driver: ch.dominant_factor_label,
    });
    if (ch.newly_high_critical) {
      toast(`⚠ ${ch.asset_id} elevated to ${ch.new_status} (${ch.previous_risk} → ${ch.new_risk})`);
    }
  });
  if (SIM.events.length > 8) SIM.events = SIM.events.slice(-8);
}

function renderSimEvents() {
  const el = $("sim-change-summary");
  if (!el) return;
  if (!SIM.active || !SIM.events || !SIM.events.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  // Freshness heartbeat: wall-clock time of the last poll plus whether that
  // update carried transitions ("steady" when the engine re-scored nothing
  // new). Refreshed on every poll so the section is visibly alive even
  // during quiet stretches between storm ramps.
  const tickTime = SIM.lastTickAt
    ? new Date(SIM.lastTickAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "";
  const meta = tickTime
    ? `updated ${esc(tickTime)} · ${SIM.lastChangeCount ? SIM.lastChangeCount + " new" : "steady"}`
    : "";
  el.innerHTML = `<div class="ev-head"><b><span class="ev-live" aria-hidden="true"></span>Live changes — backend risk state</b>` +
    (meta ? `<span class="ev-meta">${meta}</span>` : "") + `</div><ul class="sim-events">` +
    SIM.events.slice(-6).reverse().map((e) => {
      const arrow = e.dir === "rising" ? "▲" : e.dir === "falling" ? "▼" : "•";
      const dcls = e.dir === "rising" ? "ev-up" : e.dir === "falling" ? "ev-down" : "ev-flat";
      const band = e.from !== e.to
        ? `<span class="ev-band">${esc(e.from)} → ${esc(e.to)}</span>`
        : `<span>${esc(e.to)}</span>`;
      const tag = e.esc ? `<span class="ev-new">NEW ${esc(e.to).toUpperCase()} PRIORITY</span>` : "";
      return `<li><span class="ev-id">${esc(e.id)}</span>${band}` +
        `<span class="${dcls}">${arrow} ${e.oldRisk} → ${e.newRisk}</span>` +
        `<span class="ev-driver">${esc(e.driver || "")}</span>${tag}</li>`;
    }).join("") + `</ul>`;
}

// ── Scenario run / control ────────────────────────────────────────────────────

async function simLaunch() {
  // Show the scenario selector and let the operator choose, then Run
  const wrap = $("sim-scenario-select-wrap");
  if (wrap) wrap.hidden = false;
  const sel = $("sim-scenario-select");
  // Populate if not already done
  if (sel && sel.options.length === 0 && SIM_SCENARIOS.list.length > 0) {
    sel.innerHTML = SIM_SCENARIOS.list.map((s) =>
      `<option value="${esc(s.id)}">${esc(s.label)}</option>`
    ).join("");
    sel.value = SIM_SCENARIOS.default;
  }
  // Start simulation with the default/first scenario
  await simRun();
}

async function simRun() {
  const sel = $("sim-scenario-select");
  const scenarioId = (sel && sel.value) || SIM_SCENARIOS.default || "storm_surge";
  const data = await simApi("/api/scenario/run", { scenario_id: scenarioId });
  if (!data || data.error) {
    toast(data ? `Simulation error: ${data.error}` : "Could not start simulation — backend unreachable");
    return;
  }
  applySimState(data);
  await refreshSimData();
  renderSimEvents();
  switchView("maintenance");
  simStartPolling();
  toast(`Scenario started: ${data.scenario_label || scenarioId}`);
}

async function simPause() {
  const data = await simApi("/api/scenario/pause");
  if (!data) { toast("Backend unreachable"); return; }
  applySimState(data);
  renderSimEvents();
}

async function simResume() {
  const data = await simApi("/api/scenario/resume");
  if (!data) { toast("Backend unreachable"); return; }
  applySimState(data);
  renderSimEvents();
  simStartPolling();
}

async function simReset() {
  simStopPolling();
  const data = await simApi("/api/scenario/reset");
  if (!data) { toast("Backend unreachable"); return; }
  applySimState({ active: false });
  renderSimEvents();
  await refreshAllData();
  toast("Simulation reset — static demo data restored");
}

async function simStop() {
  simStopPolling();
  const data = await simApi("/api/simulation/stop");
  if (!data) { toast("Backend unreachable"); return; }
  applySimState({ active: false });
  renderSimEvents();
  await refreshAllData();
  toast("Simulation stopped — static demo data restored");
}

async function simApprovePlan() {
  const data = await simApi("/api/simulation/approve", { approved_by: "operator" });
  if (!data || data.error) { toast(data ? data.error : "Backend unreachable"); return; }
  SIM.planPending = false;
  SIM.planApproved = true;
  renderSimBanner();
  await refreshSimPlan();
  toast(`Maintenance plan approved (${data.tasks} tasks) — crew plan updated`);
}

async function simRejectPlan() {
  const data = await simApi("/api/simulation/reject");
  if (!data) { toast("Backend unreachable"); return; }
  SIM.planPending = false;
  renderSimBanner();
  toast("Pending plan rejected — previous plan retained");
}

// ── Auto-poll loop ────────────────────────────────────────────────────────────

function simStartPolling() {
  simStopPolling();
  SIM._pollTimer = setInterval(simPoll, 2500);
}

function simStopPolling() {
  if (SIM._pollTimer != null) {
    clearInterval(SIM._pollTimer);
    SIM._pollTimer = null;
  }
}

async function simPoll() {
  if (!SIM.active) { simStopPolling(); return; }
  try {
    const data = await fetch("/api/scenario/tick").then((r) => r.ok ? r.json() : null);
    if (!data) return;
    applySimState(data);
    // If pressure/phase changed, also refresh assets
    if (data.running) {
      await refreshSimData();
    }
    renderSimEvents();
    if (data.completed && !SIM._completedNotified) {
      SIM._completedNotified = true;
      simStopPolling();
      toast(`Scenario complete: ${SIM.scenarioLabel || "simulation"}`);
    }
  } catch (e) { /* keep existing state on network error */ }
}

// ── State application ─────────────────────────────────────────────────────────

function applySimState(data) {
  if (!data) return;
  const wasActive = SIM.active;
  SIM.active = data.active || false;
  if (!SIM.active) {
    SIM.flash = {};
    SIM.events = [];
    SIM.lastTickAt = null;
    SIM.lastChangeCount = 0;
  } else {
    SIM.lastTickAt = Date.now();
    SIM.lastChangeCount = (data.changes || []).length;
    queueSimFlashes(data.changes);
    pushSimEvents(data.changes);
  }
  SIM.scenarioActive = data.scenario_active || false;
  SIM.scenarioId = data.scenario_id || null;
  SIM.scenarioLabel = data.scenario_label || null;
  SIM.phase = data.phase || null;
  SIM.phaseIndex = data.phase_index != null ? data.phase_index : null;
  SIM.phaseLabel = data.phase_label || null;
  SIM.phaseDesc = data.phase_description || null;
  SIM.totalPhases = data.total_phases || 0;
  SIM.stepIndex = data.step_index != null ? data.step_index : null;
  SIM.totalSteps = data.total_steps != null ? data.total_steps : null;
  SIM.stepProgress = data.step_progress || 0;
  SIM.stepElapsedS = data.step_elapsed_s || 0;
  SIM.stepDurationS = data.step_duration_s || 0;
  SIM.elapsedS = data.elapsed_s || 0;
  SIM.running = data.running || false;
  SIM.paused = data.paused || false;
  SIM.completed = data.completed || false;
  SIM.planPending = data.plan_pending_approval || false;
  SIM.planApproved = data.plan_approved || false;
  SIM.liveWeather = data.live_weather_base || false;
  SIM._completedNotified = SIM._completedNotified && SIM.completed;
  if (!SIM.active && wasActive) simStopPolling();
  renderSimBanner();
}

function renderSimBanner() {
  const banner = $("sim-banner");
  if (!banner) return;
  if (!SIM.active) {
    banner.hidden = true;
    const pw = $("sim-progress-wrap");
    if (pw) pw.hidden = true;
    return;
  }
  banner.hidden = false;

  // Phase label + description
  $("sim-phase-label").textContent = SIM.phaseLabel || "Simulation active";
  const desc = document.querySelector("#sim-phase-desc");
  if (desc) desc.textContent = SIM.phaseDesc || "";

  // Scenario badge
  const badge = $("sim-scenario-badge");
  if (badge) {
    if (SIM.scenarioLabel) {
      badge.textContent = SIM.scenarioLabel;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  // Step/phase counter
  const counter = $("sim-phase-counter");
  if (counter) {
    if (SIM.scenarioActive && SIM.stepIndex != null && SIM.totalSteps) {
      counter.textContent = `Step ${SIM.stepIndex + 1}/${SIM.totalSteps}`;
    } else if (SIM.phaseIndex != null && SIM.totalPhases) {
      counter.textContent = `Phase ${SIM.phaseIndex + 1}/${SIM.totalPhases}`;
    } else {
      counter.textContent = "";
    }
  }

  // Progress bar
  const pw = $("sim-progress-wrap");
  if (pw) {
    pw.hidden = !SIM.scenarioActive;
    if (SIM.scenarioActive) {
      const fill = $("sim-progress-fill");
      const label = $("sim-progress-label");
      if (fill) fill.style.width = `${Math.round((SIM.stepProgress || 0) * 100)}%`;
      if (label) {
        const pct = Math.round((SIM.stepProgress || 0) * 100);
        const durS = SIM.stepDurationS || 0;
        const remS = Math.max(0, Math.round(durS - (SIM.stepElapsedS || 0)));
        label.textContent = SIM.completed
          ? "✓ Scenario complete"
          : SIM.paused ? `Paused · ${pct}%`
          : `${pct}% · ~${remS}s remaining`;
      }
    }
  }

  // Animate the dot
  const dot = $("sim-dot");
  if (dot) {
    dot.classList.toggle("sim-dot-pulse", SIM.running);
    dot.classList.toggle("sim-dot-paused", SIM.paused);
    dot.classList.toggle("sim-dot-done", SIM.completed);
  }

  // Run/Pause/Resume button visibility
  const btnRun = $("sim-run");
  const btnPause = $("sim-pause");
  const btnResume = $("sim-resume");
  if (btnRun) btnRun.hidden = SIM.running || SIM.paused || SIM.completed;
  if (btnPause) btnPause.hidden = !SIM.running || SIM.completed;
  if (btnResume) btnResume.hidden = !SIM.paused || SIM.completed;

  // Approval bar
  const approvalBar = $("sim-approval-bar");
  if (approvalBar) approvalBar.hidden = !SIM.planPending;
}

async function refreshSimData() {
  // Refresh assets + maintenance from live-simulation-adjusted backend data.
  // While a simulation is active the maintenance view is served the
  // deterministic scheduler plan (/api/simulation/plan: urgency, crew
  // assignments, reorder flags, per-task deltas) instead of the plain
  // static ranking — that is what makes the plan visibly react.
  try {
    const [d1, d2] = await Promise.all([
      api("/api/assets"),
      api(SIM.active ? "/api/simulation/plan" : "/api/priorities"),
    ]);
    S.assets = d1.assets;
    if (d2 && d2.maintenance_plan) S.priorities = d2;
  } catch (e) { /* keep existing */ }
  refreshFiltered();
  renderSide();       // keep the selected asset's detail in sync
  renderPlan();
  if (S._modalAsset && !$("modal-backdrop").classList.contains("hidden")) {
    renderModalBody(S._modalAsset);  // no focus change — pure data sync
  }
  S._lastRisk = riskSnapshot(S.assets);
}

async function refreshSimPlan() {
  try {
    const plan = await api("/api/simulation/plan");
    if (plan && plan.maintenance_plan) {
      S.priorities = plan; renderPlan();
    }
  } catch (e) { /* keep existing */ }
}

async function refreshAllData() {
  try {
    const [d1, d2, d3] = await Promise.all([
      api("/api/assets"), api("/api/summary"), api("/api/priorities"),
    ]);
    S.assets = d1.assets; S.summary = d2; S.priorities = d3;
  } catch (e) { /* keep existing */ }
  renderAll();
  if (S._modalAsset && !$("modal-backdrop").classList.contains("hidden")) {
    renderModalBody(S._modalAsset);
  }
  S._lastRisk = riskSnapshot(S.assets);
}

async function loadScenarios() {
  try {
    const data = await api("/api/scenario/list");
    if (!data || !data.scenarios) return;
    SIM_SCENARIOS.list = data.scenarios;
    SIM_SCENARIOS.default = data.default || "storm_surge";
    const sel = $("sim-scenario-select");
    if (sel) {
      sel.innerHTML = data.scenarios.map((s) =>
        `<option value="${esc(s.id)}">${esc(s.label)}</option>`
      ).join("");
      sel.value = SIM_SCENARIOS.default;
    }
  } catch (e) { /* offline — use defaults */ }
}

function initSimControls() {
  const launch = $("sim-launch");
  if (launch) launch.onclick = simLaunch;
  const run = $("sim-run");
  if (run) run.onclick = simRun;
  const pause = $("sim-pause");
  if (pause) pause.onclick = simPause;
  const resume = $("sim-resume");
  if (resume) resume.onclick = simResume;
  const reset = $("sim-reset");
  if (reset) reset.onclick = simReset;
  const stop = $("sim-stop");
  if (stop) stop.onclick = simStop;
  const approve = $("sim-approve");
  if (approve) approve.onclick = simApprovePlan;
  const reject = $("sim-reject");
  if (reject) reject.onclick = simRejectPlan;
  // Show scenario select on launch area click
  const sel = $("sim-scenario-select-wrap");
  const selEl = $("sim-scenario-select");
  if (selEl) selEl.onchange = () => {};  // no-op; value used on simRun
}

document.addEventListener("DOMContentLoaded", () => { init(); initSimControls(); loadScenarios(); });