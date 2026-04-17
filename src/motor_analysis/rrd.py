from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
from rerun.recording import load_recording


SCALAR_COMPONENT = "Scalars:scalars"


@dataclass(frozen=True)
class ScalarStream:
    """One scalar entity from the Rerun recording, aligned to a shared time origin."""

    path: str
    time_s: np.ndarray
    value: np.ndarray
    tick: np.ndarray

    @property
    def duration_s(self) -> float:
        if len(self.time_s) < 2:
            return 0.0
        return float(self.time_s[-1] - self.time_s[0])

    @property
    def median_dt_s(self) -> float:
        if len(self.time_s) < 2:
            return float("nan")
        return float(np.median(np.diff(self.time_s)))


def load_scalar_streams(path_to_rrd: str | Path) -> dict[str, ScalarStream]:
    """
    Load all Rerun `Scalars` streams from the recording.

    The Rerun SDK exposes physical chunks. This function stitches the chunks for
    each scalar entity into sorted numpy arrays and converts absolute nanosecond
    timestamps into seconds relative to the first scalar sample in the file.
    """

    recording = load_recording(path_to_rrd)
    raw: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}

    for chunk in recording.chunks():
        if chunk.is_static:
            continue

        batch = chunk.to_record_batch()
        if SCALAR_COMPONENT not in batch.schema.names:
            continue

        time_ns = batch.column("log_time").cast(pa.int64()).to_numpy(zero_copy_only=False)
        tick = batch.column("log_tick").to_numpy(zero_copy_only=False)
        value = _flatten_scalar_column(batch.column(SCALAR_COMPONENT), batch.num_rows)
        raw.setdefault(chunk.entity_path, []).append((time_ns, tick, value))

    if not raw:
        raise ValueError(f"No scalar streams found in {path_to_rrd}")

    origin_ns = min(int(parts[0][0]) for chunks in raw.values() for parts in chunks)
    streams: dict[str, ScalarStream] = {}

    for entity_path, chunks in raw.items():
        time_ns = np.concatenate([part[0] for part in chunks])
        tick = np.concatenate([part[1] for part in chunks])
        value = np.concatenate([part[2] for part in chunks])
        order = np.argsort(time_ns, kind="stable")

        streams[entity_path] = ScalarStream(
            path=entity_path,
            time_s=(time_ns[order].astype(np.float64) - float(origin_ns)) * 1e-9,
            value=value[order].astype(np.float64),
            tick=tick[order].astype(np.int64),
        )

    return streams


def _flatten_scalar_column(column: pa.Array, expected_rows: int) -> np.ndarray:
    """
    Rerun stores Scalars as `List(Float64)`, usually one value per row.

    The fast path uses the child values directly. The fallback handles any
    unexpected empty or multi-value rows by taking the first value and using NaN
    for empty rows.
    """

    values = column.values.to_numpy(zero_copy_only=False)
    if len(values) == expected_rows:
        return values

    flattened = np.empty(expected_rows, dtype=np.float64)
    for idx, item in enumerate(column.to_pylist()):
        flattened[idx] = item[0] if item else np.nan
    return flattened
