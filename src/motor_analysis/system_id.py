from __future__ import annotations

import html
from pathlib import Path

import numpy as np

from .analysis import AXES, AnalysisConfig, zero_order_hold
from .rrd import ScalarStream


def is_valid_system_id_step(
    row: dict[str, object],
    min_arrival_latency_s: float,
) -> bool:
    """
    Return True when a movement row is clean enough for step-response analysis.

    The filter intentionally matches the report's step-target arrival plot:
    one dominant target jump and no near-zero final-arrival artifacts.
    """

    arrival = _float(row.get("arrival_latency_s", np.nan))
    return (
        int(row.get("is_step_like_target", 0)) == 1
        and np.isfinite(arrival)
        and arrival >= min_arrival_latency_s
    )


def filter_system_id_step_rows(
    movement_rows: list[dict[str, object]],
    min_arrival_latency_s: float,
) -> list[dict[str, object]]:
    """Preserve the exact filtered step-response subset used for system ID."""

    return [
        row
        for row in movement_rows
        if is_valid_system_id_step(row, min_arrival_latency_s)
    ]


def build_system_id_step_response_rows(
    streams: dict[str, ScalarStream],
    movement_rows: list[dict[str, object]],
    config: AnalysisConfig,
) -> list[dict[str, object]]:
    """Add velocity metrics to the filtered step-target movement rows."""

    rows: list[dict[str, object]] = []
    for row in filter_system_id_step_rows(movement_rows, config.system_id_min_arrival_latency_s):
        axis = str(row["axis"])
        current = streams[f"/motors/position/{axis}/current"]
        command_time_s = float(row["end_time_s"])
        metrics = _velocity_metrics_for_step(current, row, command_time_s, config)
        rows.append(
            {
                "axis": axis,
                "episode_idx": int(row["episode_idx"]),
                "command_time_s": command_time_s,
                "start_time_s": float(row["start_time_s"]),
                "target_hold_after_s": float(row["target_hold_after_s"]),
                "starting_target_deg": float(row["initial_target_deg"]),
                "final_target_deg": float(row["final_target_deg"]),
                "signed_step_deg": float(row["delta_deg"]),
                "step_size_deg": float(row["magnitude_deg"]),
                "starting_actual_deg": float(zero_order_hold(current.time_s, current.value, np.array([command_time_s]))[0]),
                "arrival_latency_s": float(row["arrival_latency_s"]),
                "settling_time_s": float(row["settling_time_s"]),
                "trajectory_lag_s": float(row["trajectory_lag_s"]),
                **metrics,
            }
        )
    return rows


def write_system_id_page(
    output_dir: Path,
    system_id_rows: list[dict[str, object]],
    config: AnalysisConfig,
) -> None:
    """Write a separate system-identification exploration page."""

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "system_id.html").write_text(
        _system_id_html(system_id_rows, config),
        encoding="utf-8",
    )


def _velocity_metrics_for_step(
    current: ScalarStream,
    row: dict[str, object],
    command_time_s: float,
    config: AnalysisConfig,
) -> dict[str, object]:
    response_window_s = min(float(row["target_hold_after_s"]), config.response_max_s)
    window_end_s = command_time_s + response_window_s
    mask = (current.time_s >= command_time_s) & (current.time_s <= window_end_s)
    t = current.time_s[mask]
    v = current.value[mask]

    if len(t) < 3:
        return {
            "response_window_s": response_window_s,
            "velocity_sample_count": int(max(0, len(t) - 1)),
            "max_velocity_magnitude_deg_s": np.nan,
            "max_velocity_time_s": np.nan,
            "velocity_rise_time_90_s": np.nan,
            "signed_velocity_at_peak_deg_s": np.nan,
            "starting_velocity_deg_s": np.nan,
        }

    dt = np.diff(t)
    good_dt = dt > 0.0
    if not np.any(good_dt):
        return {
            "response_window_s": response_window_s,
            "velocity_sample_count": 0,
            "max_velocity_magnitude_deg_s": np.nan,
            "max_velocity_time_s": np.nan,
            "velocity_rise_time_90_s": np.nan,
            "signed_velocity_at_peak_deg_s": np.nan,
            "starting_velocity_deg_s": np.nan,
        }

    velocity_t = (t[:-1][good_dt] + t[1:][good_dt]) / 2.0
    velocity = np.diff(v)[good_dt] / dt[good_dt]
    velocity_abs = np.abs(velocity)
    finite = np.isfinite(velocity_abs)
    if not np.any(finite):
        return {
            "response_window_s": response_window_s,
            "velocity_sample_count": int(len(velocity)),
            "max_velocity_magnitude_deg_s": np.nan,
            "max_velocity_time_s": np.nan,
            "velocity_rise_time_90_s": np.nan,
            "signed_velocity_at_peak_deg_s": np.nan,
            "starting_velocity_deg_s": np.nan,
        }

    finite_indices = np.flatnonzero(finite)
    peak_local_idx = finite_indices[int(np.argmax(velocity_abs[finite]))]
    max_velocity = float(velocity_abs[peak_local_idx])
    peak_time_s = float(velocity_t[peak_local_idx] - command_time_s)
    signed_peak_velocity = float(velocity[peak_local_idx])
    starting_velocity = float(velocity[finite_indices[0]])

    rise_time_90_s = np.nan
    if max_velocity > 0.0:
        threshold = 0.90 * max_velocity
        rise_candidates = finite_indices[velocity_abs[finite] >= threshold]
        if len(rise_candidates):
            rise_time_90_s = float(velocity_t[int(rise_candidates[0])] - command_time_s)

    return {
        "response_window_s": response_window_s,
        "velocity_sample_count": int(len(velocity)),
        "max_velocity_magnitude_deg_s": max_velocity,
        "max_velocity_time_s": peak_time_s,
        "velocity_rise_time_90_s": rise_time_90_s,
        "signed_velocity_at_peak_deg_s": signed_peak_velocity,
        "starting_velocity_deg_s": starting_velocity,
    }


def _system_id_html(
    rows: list[dict[str, object]],
    config: AnalysisConfig,
) -> str:
    min_latency_ms = config.system_id_min_arrival_latency_s * 1000.0
    finite_velocity = _finite_count(rows, "max_velocity_magnitude_deg_s")
    finite_rise = _finite_count(rows, "velocity_rise_time_90_s")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>System ID Step Response Exploration</title>
  <style>
    :root {{
      --ink: #20242a;
      --muted: #5a6675;
      --panel: #ffffff;
      --line: #c9d1db;
      --grid: #e5eaf0;
    }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #f6f8fa; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 30px 18px 54px; }}
    h1 {{ margin: 0 0 8px; font-size: 32px; }}
    h2 {{ margin-top: 30px; font-size: 22px; }}
    p {{ color: var(--muted); line-height: 1.45; max-width: 940px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 22px 0 28px; }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; }}
    .metric {{ display: block; font-size: 24px; font-weight: 700; }}
    .label {{ color: var(--muted); font-size: 13px; }}
    figure {{ margin: 22px 0 32px; }}
    .plot {{ width: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; display: block; }}
    figcaption {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
    table {{ border-collapse: collapse; width: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; display: block; overflow-x: auto; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #e2e7ee; text-align: left; white-space: nowrap; font-size: 13px; }}
    th {{ background: #eef2f6; }}
    code {{ background: #e9edf2; padding: 1px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
<main>
  <h1>System ID Step Response Exploration</h1>
  <p>This page uses the preserved step-response subset written to <code>system_id_step_responses.csv</code>. The subset requires a step-like target and excludes final-position arrival measurements below {min_latency_ms:.0f} ms, because those near-zero points were ruled out as invalid for this data.</p>
  <section class="grid">
    <div class="card"><span class="metric">{len(rows)}</span><span class="label">filtered step responses</span></div>
    <div class="card"><span class="metric">{finite_velocity}</span><span class="label">finite peak-velocity measurements</span></div>
    <div class="card"><span class="metric">{finite_rise}</span><span class="label">finite 90% velocity-rise measurements</span></div>
  </section>
  <h2>Velocity Capacity</h2>
  <p>Maximum velocity magnitude is the peak absolute finite-difference velocity of the actual motor position after the final target update and before the next target update, capped by the configured response window.</p>
  <figure>
    {_svg_scatter(rows, "step_size_deg", "max_velocity_magnitude_deg_s", "Maximum velocity magnitude vs step size magnitude", "step size magnitude (deg)", "max velocity magnitude (deg/s)")}
    <figcaption>Look for saturation: if peak velocity stops increasing as step size grows, the motor is velocity-limited over larger commands.</figcaption>
  </figure>
  <h2>Velocity Rise vs Starting Angle</h2>
  <p>Velocity rise time is the first post-command time where absolute velocity reaches 90% of that movement's own peak velocity. Plotting it against starting actual angle can expose angle-dependent delay, backlash, or slop.</p>
  <figure>
    {_svg_scatter(rows, "starting_actual_deg", "velocity_rise_time_90_s", "90% velocity-rise time vs starting actual angle", "starting actual angle (deg)", "time to 90% peak velocity (ms)", y_scale=1000.0, y_limits=(0.0, 150.0))}
    <figcaption>Clusters with longer rise time at specific starting angles are candidates for mechanical slop or angle-dependent load.</figcaption>
  </figure>
  <h2>Filtered Dataset Preview</h2>
  {_html_table(rows[:20], ["axis", "episode_idx", "starting_actual_deg", "step_size_deg", "arrival_latency_s", "max_velocity_magnitude_deg_s", "velocity_rise_time_90_s"])}
</main>
</body>
</html>
"""


def _svg_scatter(
    rows: list[dict[str, object]],
    x_key: str,
    y_key: str,
    title: str,
    x_label: str,
    y_label: str,
    y_scale: float = 1.0,
    y_limits: tuple[float, float] | None = None,
) -> str:
    points = [
        row
        for row in rows
        if np.isfinite(_float(row.get(x_key, np.nan)))
        and np.isfinite(_float(row.get(y_key, np.nan)))
    ]
    if not points:
        return "<p>No finite points available.</p>"

    sampled: list[dict[str, object]] = []
    max_points_per_axis = 1200
    for axis in AXES:
        axis_points = [row for row in points if row["axis"] == axis]
        if len(axis_points) > max_points_per_axis:
            idx = np.linspace(0, len(axis_points) - 1, max_points_per_axis).astype(int)
            sampled.extend(axis_points[int(i)] for i in idx)
        else:
            sampled.extend(axis_points)

    x_values = np.array([_float(row[x_key]) for row in sampled], dtype=float)
    y_values = np.array([_float(row[y_key]) * y_scale for row in sampled], dtype=float)
    x_min, x_max = _range_with_padding(x_values, lower_zero=x_key == "step_size_deg")
    if y_limits is None:
        y_min, y_max = _range_with_padding(y_values, lower_zero=True)
    else:
        y_min, y_max = y_limits

    width, height = 920, 380
    left, right, top, bottom = 68, 22, 46, 56
    plot_w = width - left - right
    plot_h = height - top - bottom

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_w

    def sy(value: float) -> float:
        return top + plot_h - (value - y_min) / (y_max - y_min) * plot_h

    colors = {"pitch": "#1769aa", "yaw": "#c94f2d"}
    circles = []
    for row in sampled:
        axis = str(row["axis"])
        y_value = _float(row[y_key]) * y_scale
        if not y_min <= y_value <= y_max:
            continue
        circles.append(
            f'<circle cx="{sx(_float(row[x_key])):.2f}" cy="{sy(y_value):.2f}" '
            f'r="2.3" fill="{colors[axis]}" opacity="0.42"/>'
        )

    x_ticks = _ticks(x_min, x_max, 6)
    y_ticks = _ticks(y_min, y_max, 5)
    grid = []
    labels = []
    for tick in x_ticks:
        x = sx(tick)
        grid.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_h}" stroke="#e5eaf0"/>')
        labels.append(f'<text x="{x:.2f}" y="{height - 22}" text-anchor="middle" font-size="11" fill="#5a6675">{_fmt_tick(tick)}</text>')
    for tick in y_ticks:
        y = sy(tick)
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e5eaf0"/>')
        labels.append(f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" font-size="11" fill="#5a6675">{_fmt_tick(tick)}</text>')

    return f"""
<svg class="plot" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <text x="{left}" y="25" font-size="17" font-weight="700" fill="#20242a">{html.escape(title)}</text>
  <text x="{left + plot_w - 170}" y="25" font-size="12" fill="#1769aa">pitch</text>
  <text x="{left + plot_w - 105}" y="25" font-size="12" fill="#c94f2d">yaw</text>
  {''.join(grid)}
  <line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#718096"/>
  <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#718096"/>
  {''.join(circles)}
  {''.join(labels)}
  <text x="{left + plot_w / 2}" y="{height - 4}" text-anchor="middle" font-size="12" fill="#5a6675">{html.escape(x_label)}</text>
  <text transform="translate(16 {top + plot_h / 2}) rotate(-90)" text-anchor="middle" font-size="12" fill="#5a6675">{html.escape(y_label)}</text>
</svg>
"""


def _range_with_padding(values: np.ndarray, lower_zero: bool) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if not len(finite):
        return 0.0, 1.0
    lo = 0.0 if lower_zero else float(np.nanpercentile(finite, 1.0))
    hi = float(np.nanpercentile(finite, 99.0))
    if not lower_zero:
        span = hi - lo
        lo -= 0.08 * span
        hi += 0.08 * span
    else:
        hi *= 1.08
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _ticks(start: float, stop: float, count: int) -> list[float]:
    if stop <= start:
        return [start]
    return [float(value) for value in np.linspace(start, stop, count)]


def _finite_count(rows: list[dict[str, object]], key: str) -> int:
    return sum(1 for row in rows if np.isfinite(_float(row.get(key, np.nan))))


def _html_table(rows: list[dict[str, object]], columns: list[str]) -> str:
    if not rows:
        return "<p>No rows.</p>"
    head = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(_fmt_cell(row.get(column, '')))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _fmt_cell(value: object) -> str:
    if isinstance(value, float):
        if not np.isfinite(value):
            return ""
        if abs(value) >= 1000:
            return f"{value:.1f}"
        if abs(value) >= 10:
            return f"{value:.2f}"
        if abs(value) >= 1:
            return f"{value:.3f}"
        return f"{value:.4f}"
    return str(value)


def _fmt_tick(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan
