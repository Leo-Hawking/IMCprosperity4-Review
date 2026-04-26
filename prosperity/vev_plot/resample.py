"""Plotly resample —— 劫持 go.Figure.show, 对过长的 scatter trace 做均匀下采样。

用法:
    from vev_plot.resample import enable_plotly_resample, no_resample
    enable_plotly_resample(max_points=4000)  # 全局开
    with no_resample():                       # 局部关，做 zoom 精确查看
        fig.show()
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
    was_on = _ORIG_SHOW is not None
    max_pts = _MAX_POINTS
    if was_on:
        disable_plotly_resample()
    try:
        yield
    finally:
        if was_on:
            enable_plotly_resample(max_pts)


def _indices(n: int, target: int) -> list[int]:
    if n <= target:
        return list(range(n))
    step = n / target
    idx = [int(i * step) for i in range(target)]
    if idx[-1] != n - 1:
        idx[-1] = n - 1
    seen: set[int] = set()
    out: list[int] = []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _index_array(arr, idx):
    if arr is None:
        return None
    try:
        import numpy as np
        if isinstance(arr, np.ndarray):
            return arr[idx]
        if isinstance(arr, (list, tuple)):
            return [arr[i] for i in idx]
        return [list(arr)[i] for i in idx]
    except Exception:
        return arr


def _downsample_trace(trace) -> None:
    if getattr(trace, "type", None) not in ("scatter", "scattergl"):
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
    idx = _indices(n, _MAX_POINTS)

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

    marker = getattr(trace, "marker", None)
    if marker is not None:
        for a in ("size", "color", "opacity"):
            v = getattr(marker, a, None)
            if v is None:
                continue
            try:
                if len(v) == n:
                    setattr(marker, a, _index_array(v, idx))
            except TypeError:
                continue


def _patched_show(self, *args, **kwargs):
    for tr in self.data:
        _downsample_trace(tr)
    assert _ORIG_SHOW is not None
    return _ORIG_SHOW(self, *args, **kwargs)
