"""Rainfall event context extraction for diagnostic evidence packages."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from workflow_agents.common.schemas import RAINFALL_CONTEXT_SCHEMA_NAME, WORKFLOW_SCHEMA_VERSION


LIGHT_RAIN_TOTAL_MM = 10.0
MODERATE_RAIN_TOTAL_MM = 25.0
HEAVY_RAIN_TOTAL_MM = 50.0
RAINSTORM_TOTAL_MM = 100.0
SHORT_DURATION_HEAVY_INTENSITY_MM_H = 20.0
SHORT_DURATION_MAX_MINUTES = 180.0


def _empty_context(event_name: str, rainfall_file: str = "", reason: str = "missing_rainfall_file") -> dict[str, Any]:
    return {
        "schema_name": RAINFALL_CONTEXT_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "event_name": event_name,
        "rainfall_file": rainfall_file,
        "unit": "mm/h",
        "total_rainfall_mm": None,
        "rainfall_duration_min": None,
        "effective_rainfall_duration_min": None,
        "max_intensity_mm_per_h": None,
        "mean_intensity_mm_per_h": None,
        "rainfall_peak_time": None,
        "intensity_class": "unknown",
        "short_duration_heavy_rainfall": False,
        "classification_basis": "internal_thresholds_pending_local_standard_calibration",
        "classification_note": reason,
        "rainfall_points": 0,
        "timeline_status": "unavailable", "rainfall_series": [],
        "wet_periods": [], "rainfall_peaks": [], "dry_intervals": [],
        "history_before_record": "not_provided",
    }


def _read_rainfall_points(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    unit = "mm/h"
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("单位") or line.lower().startswith("unit"):
            if "mm/h" in line:
                unit = "mm/h"
            else:
                raise ValueError('Rainfall timeline requires declared mm/h input')
            continue
        if "," not in line:
            if line.startswith('#'):
                continue
            raise ValueError('Unparseable rainfall input line')
        time_text, value_text = line.rsplit(",", 1)
        if time_text.strip().lower() in {'timestamp', 'datetime', 'time', '时间'}:
            continue
        timestamp = pd.to_datetime(time_text.strip(), errors="coerce")
        value = pd.to_numeric(value_text.strip(), errors="coerce")
        if pd.isna(timestamp) or pd.isna(value):
            raise ValueError('Invalid rainfall timestamp or intensity; missing is not dry weather')
        rows.append({"timestamp": timestamp, "intensity_mm_per_h": float(value)})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "intensity_mm_per_h", "unit"])
    if df.timestamp.duplicated().any() or not df.timestamp.is_monotonic_increasing:
        raise ValueError('Rainfall timestamps must be ordered and unique')
    df["unit"] = unit
    return df.reset_index(drop=True)


def _stepwise_total_rainfall_mm(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    total = 0.0
    timestamps = df["timestamp"].tolist()
    intensities = df["intensity_mm_per_h"].tolist()
    for idx in range(len(df) - 1):
        delta_hours = (timestamps[idx + 1] - timestamps[idx]).total_seconds() / 3600.0
        if delta_hours > 0:
            total += float(intensities[idx]) * delta_hours
    return float(total)


def _stepwise_positive_duration_min(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    duration = 0.0
    timestamps = df["timestamp"].tolist()
    intensities = df["intensity_mm_per_h"].tolist()
    for idx in range(len(df) - 1):
        delta_minutes = (timestamps[idx + 1] - timestamps[idx]).total_seconds() / 60.0
        if delta_minutes > 0 and float(intensities[idx]) > 0.0:
            duration += delta_minutes
    return float(duration)


def _classify_rainfall(total_mm: float, duration_min: float, max_intensity: float) -> tuple[str, bool, str]:
    short_heavy = max_intensity >= SHORT_DURATION_HEAVY_INTENSITY_MM_H and duration_min <= SHORT_DURATION_MAX_MINUTES
    if short_heavy:
        return (
            "short_duration_heavy_rainfall",
            True,
            f"max_intensity >= {SHORT_DURATION_HEAVY_INTENSITY_MM_H} mm/h and duration <= {SHORT_DURATION_MAX_MINUTES} min",
        )
    if total_mm >= RAINSTORM_TOTAL_MM:
        return "heavy_rainstorm", False, f"total_rainfall >= {RAINSTORM_TOTAL_MM} mm"
    if total_mm >= HEAVY_RAIN_TOTAL_MM:
        return "rainstorm", False, f"total_rainfall >= {HEAVY_RAIN_TOTAL_MM} mm"
    if total_mm >= MODERATE_RAIN_TOTAL_MM:
        return "heavy_rain", False, f"total_rainfall >= {MODERATE_RAIN_TOTAL_MM} mm"
    if total_mm >= LIGHT_RAIN_TOTAL_MM:
        return "moderate_rain", False, f"total_rainfall >= {LIGHT_RAIN_TOTAL_MM} mm"
    return "light_rain", False, f"total_rainfall < {LIGHT_RAIN_TOTAL_MM} mm"


def rainfall_timeline(df: pd.DataFrame) -> dict[str, Any]:
    """Complete supplied input points and descriptive wet intervals/peaks."""
    times = [pd.Timestamp(t) for t in df.timestamp]
    values = [float(v) for v in df.intensity_mm_per_h]
    if not values:
        return {'timeline_status': 'unavailable', 'rainfall_series': [],
                'wet_periods': [], 'dry_intervals': [], 'rainfall_peaks': [],
                'history_before_record': 'not_provided'}
    if any(not pd.notna(v) or v < 0 or v == float('inf') for v in values):
        raise ValueError('Rainfall intensity must be finite and nonnegative')
    def stamp(t):
        return t.isoformat(sep=' ')
    wet, dry = [], []
    for i in range(len(times)-1):
        target = wet if values[i] > 0 else dry
        amount = values[i] * (times[i+1]-times[i]).total_seconds()/3600
        if target and target[-1]['end'] == stamp(times[i]):
            target[-1]['end'] = stamp(times[i+1])
            target[-1]['rainfall_mm'] += amount
        else:
            target.append({'start': stamp(times[i]), 'end': stamp(times[i+1]), 'rainfall_mm': amount})
    peaks, i = [], 0
    while i < len(values)-1:
        j = i
        while j+1 < len(values)-1 and values[j+1] == values[i]:
            j += 1
        before = values[i-1] if i else None
        after = values[j+1]
        if values[i] > 0 and (before is None or values[i] > before) and values[i] > after:
            peaks.append({'peak_time': stamp(times[i]), 'plateau_end': stamp(times[j+1]),
                          'intensity_mm_per_h': values[i], 'left_boundary_peak': before is None})
        i = j+1
    return {'timeline_status': 'available',
            'rainfall_series': [{'time': stamp(t), 'intensity_mm_per_h': v} for t,v in zip(times,values)],
            'series_semantics': 'Each supplied value applies until the next supplied timestamp; no interval is invented after the final point.',
            'wet_periods': wet, 'dry_intervals': dry, 'rainfall_peaks': peaks,
            'last_input_point_positive': values[-1] > 0,
            'history_before_record': 'not_provided',
            'peak_interpretation': 'Local maxima and contiguous positive intervals describe this input; neither automatically identifies independent storms.'}


def event_rainfall_position(context: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Locate an overflow on the supplied rain clock; do not infer retained water."""
    base = {'method': 'event_rainfall_position_v2', 'rainfall_evidence_id': 'CTX_RAINFALL_TIMELINE',
            'event_start': event['start'], 'event_end': event['end'],
            'interpretation': 'Earlier rain is temporal context, not proof of residual in-network water.'}
    if context.get('timeline_status') != 'available':
        return {**base, 'status': 'unavailable', 'reason': 'No complete supplied rainfall timeline'}
    start = pd.Timestamp(event['start'])
    series = context['rainfall_series']
    first, last = (pd.Timestamp(series[k]['time']) for k in (0,-1))
    wet = next((p for p in context['wet_periods'] if pd.Timestamp(p['start']) <= start < pd.Timestamp(p['end'])), None)
    phase = ('before_rainfall_record' if start < first else 'after_rainfall_record' if start > last else
             'at_final_input_point_no_following_interval' if start == last else
             'during_recorded_wet_period' if wet else 'during_recorded_zero_rain_interval')
    earlier_peaks = [p for p in context['rainfall_peaks'] if pd.Timestamp(p['peak_time']) <= start]
    later_peaks = [p for p in context['rainfall_peaks'] if pd.Timestamp(p['peak_time']) > start]
    completed = [p for p in context['wet_periods'] if pd.Timestamp(p['end']) <= start]
    # Elapsed time from the preceding rain end can include the current wet
    # period. It must not be labelled as the duration of a zero-rain gap.
    dry = next((p for p in context['dry_intervals'] if wet and p['end'] == wet['start']), None)
    dry_gap = ({**dry, 'duration_minutes': (pd.Timestamp(dry['end'])-pd.Timestamp(dry['start'])).total_seconds()/60}
               if dry else None)
    amount = 0.
    for a,b in zip(series,series[1:]):
        left,right = pd.Timestamp(a['time']),min(start,pd.Timestamp(b['time']))
        if right > left:
            amount += a['intensity_mm_per_h']*(right-left).total_seconds()/3600
    return {**base, 'status': 'available', 'phase_at_start': phase,
            'elapsed_minutes_from_record_start': (start-first).total_seconds()/60,
            'recorded_rain_before_event_mm': amount, 'current_wet_period': wet,
            'minutes_since_current_wet_period_start': (start-pd.Timestamp(wet['start'])).total_seconds()/60 if wet else None,
            'zero_rain_interval_before_current_wet_period': dry_gap,
            'prior_peak_count': len(earlier_peaks), 'latest_prior_peak': earlier_peaks[-1] if earlier_peaks else None,
            'peak_before_latest_prior_peak': earlier_peaks[-2] if len(earlier_peaks)>1 else None,
            'minutes_since_latest_prior_peak': (start-pd.Timestamp(earlier_peaks[-1]['peak_time'])).total_seconds()/60 if earlier_peaks else None,
            'next_recorded_peak': later_peaks[0] if later_peaks else None,
            'last_completed_wet_period': completed[-1] if completed else None,
            'minutes_since_last_completed_wet_period': (start-pd.Timestamp(completed[-1]['end'])).total_seconds()/60 if completed else None,
            'elapsed_from_previous_rain_end_note': 'Elapsed time can include current rainfall; it is not the zero-rain gap duration. Read the separately bounded dry interval.',
            'history_before_record': context['history_before_record']}


def build_rainfall_context(
    rainfall_file: Path,
    event_name: str = "",
    run_id: str = "",
    model_name: str = "",
    scenario_name: str = "",
) -> dict[str, Any]:
    """Parse a rainfall event file and produce diagnostic rainfall context."""
    if not rainfall_file.exists():
        return _empty_context(event_name, str(rainfall_file))

    df = _read_rainfall_points(rainfall_file)
    if df.empty:
        return _empty_context(event_name, str(rainfall_file), reason="no_parseable_rainfall_points")

    first_time = df["timestamp"].min()
    last_time = df["timestamp"].max()
    duration_min = float((last_time - first_time).total_seconds() / 60.0) if len(df) > 1 else 0.0
    effective_duration_min = _stepwise_positive_duration_min(df)

    max_idx = df["intensity_mm_per_h"].idxmax()
    max_intensity = float(df.loc[max_idx, "intensity_mm_per_h"])
    total_mm = _stepwise_total_rainfall_mm(df)
    mean_intensity = float(total_mm / (duration_min / 60.0)) if duration_min > 0 else 0.0
    intensity_class, short_heavy, classification_note = _classify_rainfall(total_mm, duration_min, max_intensity)

    context = {
        "schema_name": RAINFALL_CONTEXT_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "run_id": run_id,
        "model_name": model_name,
        "event_name": event_name or rainfall_file.stem,
        "scenario_name": scenario_name,
        "rainfall_file": str(rainfall_file),
        "unit": str(df["unit"].iloc[0]) if "unit" in df else "mm/h",
        "total_rainfall_mm": round(total_mm, 6),
        "rainfall_duration_min": round(duration_min, 6),
        "effective_rainfall_duration_min": round(effective_duration_min, 6),
        "max_intensity_mm_per_h": round(max_intensity, 6),
        "mean_intensity_mm_per_h": round(mean_intensity, 6),
        "rainfall_start": pd.Timestamp(first_time).isoformat(sep=" "),
        "rainfall_end": pd.Timestamp(last_time).isoformat(sep=" "),
        "rainfall_peak_time": pd.Timestamp(df.loc[max_idx, "timestamp"]).isoformat(sep=" "),
        "intensity_class": intensity_class,
        "short_duration_heavy_rainfall": short_heavy,
        "classification_basis": "internal_thresholds_pending_local_standard_calibration",
        "classification_note": classification_note,
        "rainfall_points": int(len(df)),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    context.update(rainfall_timeline(df))
    return context
