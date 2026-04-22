"""Plotly resample 开关 — monkey-patch go.Figure.show。

开启后，每次 fig.show() 对 scatter / scattergl trace 做均匀下采样（首尾必保留），
超过 max_points 的 trace 才处理；同步采样 x / y / customdata / text / hovertext 和
数组形式的 marker.size / marker.color / line.color。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import plotly.graph_objects as go

_ORIG_SHOW: Any = None
_MAX_POINTS: int = 4000


def enable_plotly_resample(max_points: int = 4000) -> None:
    global _ORIG_SHOW, _MAX_POINTS
    _MAX_POINTS = int(max_points)
    if _ORIG_SHOW is None:
        _ORIG_SHOW = go.Figure.show
        go.Figure.show = _patched_show  # type: ignore[assignment]


def disable_plotly_resample() -> None:
    global _ORIG_SHOW
    if _ORIG_SHOW is not None:
        go.Figure.show = _ORIG_SHOW  # type: ignore[assignment]
        _ORIG_SHOW = None


@contextmanager
def no_resample():
    """临时关闭 resample（用于 zoom 全精度渲染）。"""
    was_on = _ORIG_SHOW is not None
    max_pts = _MAX_POINTS
    if was_on:
        disable_plotly_resample()
    try:
        yield
    finally:
        if was_on:
            enable_plotly_resample(max_pts)


def _downsample_indices(n: int, target: int) -> list[int]:
    if n <= target:
        return list(range(n))
    # 均匀步长采样，保证首尾索引
    step = n / target
    idx = [int(i * step) for i in range(target)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    # 去重（以防极端情况）
    seen = set()
    out = []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _index_array(arr, idx):
    if arr is None:
        return None
    # 兼容 list / tuple / numpy / pandas
    try:
        if hasattr(arr, "__getitem__") and not isinstance(arr, (str, bytes)):
            # numpy array supports fancy indexing
            import numpy as np

            if isinstance(arr, np.ndarray):
                return arr[idx]
            if isinstance(arr, (list, tuple)):
                return [arr[i] for i in idx]
            # fallback: try iteration
            seq = list(arr)
            return [seq[i] for i in idx]
    except Exception:
        return arr
    return arr


def _maybe_downsample_marker_array(marker, idx: list[int]) -> None:
    if marker is None:
        return
    for attr in ("size", "color", "opacity"):
        v = getattr(marker, attr, None)
        if v is None:
            continue
        try:
            n = len(v)  # type: ignore[arg-type]
        except TypeError:
            continue
        if n == 0 or n != _orig_n_cache.get(id(marker), n):
            continue
        try:
            setattr(marker, attr, _index_array(v, idx))
        except Exception:
            pass


def _maybe_downsample_line_array(line, idx: list[int]) -> None:
    if line is None:
        return
    for attr in ("color", "width"):
        v = getattr(line, attr, None)
        if v is None:
            continue
        try:
            n = len(v)  # type: ignore[arg-type]
        except TypeError:
            continue
        if n == 0:
            continue
        try:
            setattr(line, attr, _index_array(v, idx))
        except Exception:
            pass


_orig_n_cache: dict[int, int] = {}


def _downsample_trace(trace) -> None:
    # 只处理 scatter / scattergl
    t_type = getattr(trace, "type", None)
    if t_type not in ("scatter", "scattergl"):
        return
    x = getattr(trace, "x", None)
    if x is None:
        return
    try:
        n = len(x)
    except TypeError:
        return
    if n <= _MAX_POINTS:
        return

    idx = _downsample_indices(n, _MAX_POINTS)

    trace.x = _index_array(x, idx)
    trace.y = _index_array(getattr(trace, "y", None), idx)

    cd = getattr(trace, "customdata", None)
    if cd is not None:
        try:
            import numpy as np

            if isinstance(cd, np.ndarray) and cd.ndim == 2:
                trace.customdata = cd[idx, :]
            else:
                trace.customdata = _index_array(cd, idx)
        except Exception:
            trace.customdata = _index_array(cd, idx)

    for attr in ("text", "hovertext", "ids"):
        v = getattr(trace, attr, None)
        if v is not None:
            try:
                if len(v) == n:
                    setattr(trace, attr, _index_array(v, idx))
            except TypeError:
                continue

    _maybe_downsample_marker_array(getattr(trace, "marker", None), idx)
    _maybe_downsample_line_array(getattr(trace, "line", None), idx)


def _patched_show(self, *args, **kwargs):
    for tr in self.data:
        _downsample_trace(tr)
    assert _ORIG_SHOW is not None  # guarded by enable
    return _ORIG_SHOW(self, *args, **kwargs)
