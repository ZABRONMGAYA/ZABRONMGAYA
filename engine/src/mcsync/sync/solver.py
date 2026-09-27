"""Global placement of many clips from pairwise measurements.

Model
-----
Clip ``i`` maps its local time ``τ`` (seconds since its first sample, by its
own clock) onto the timeline as ``T_i(τ) = x_i + (1 + r_i)·τ``. ``x_i`` is
where the clip starts and ``r_i`` is its clock-rate error relative to the
reference clip, whose clock defines the timeline (``r_ref = 0``). Measurements
are edges between nodes:

* **audio** edges from a :class:`PairwiseMatch` (reference R, target T): at
  target time ``t`` the offset is ``m`` (local times ``t`` in T and ``t + m``
  in R are the same instant), and the lag slope ``b`` gives the rate
  difference. They yield two constraints::

      r_T - r_R = b                                  (rates)
      x_T - x_R = m + r_R·(t + m) - r_T·t            (starts)

* **clock** edges: each clock domain (a jam-synced timecode generator, or one
  device's internal clock) is a virtual node ``c`` whose unknown ``x_c`` is
  where that clock's zero lies on the timeline, and ``x_clip - x_c =
  clock_start``. This is how an interrupted clip without usable audio still
  lands correctly: its device clock ties it to that device's other clips.
* **manual** edges from the user: exact constraints on the displayed starts.

Rates are solved first by weighted least squares on the audio graph (weights
``1/σ_b²``); starts are then solved the same way with the rate corrections
applied. Measuring every pair at its own overlap midpoint and correcting with
global rates keeps cycles consistent even with tens of ppm of drift over
hours, where constant offsets would disagree by tens of milliseconds.

Manual constraints are applied exactly by merging nodes (a weighted
union-find keeps each node's offset from its group root). Components are
anchored at the reference clip, or at the longest clip in components that do
not contain it.

Outlier rejection: after each solve the edge with the largest residual
relative to ``max(outlier_sigma·sigma, outlier_min_s)`` is rejected and
everything is solved again, until every remaining edge is consistent. A wrong
edge in a cycle of correct ones stands out because the correct edges agree
with each other. Bridges (edges in no cycle) always have zero residual; they
cannot be checked and are reported as-is.

Displayed start
---------------
An NLE plays a clip at nominal speed from one start position ``P``. The
misalignment at local time ``τ`` is then ``P - x - r·τ``, which is smallest
overall for ``P = x + r·D/2`` (D = duration): zero mid-clip, ``±r·D/2`` at the
ends. That is the reported ``start_s``; ``drift_ppm`` allows exact retiming.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import spsolve

from .params import DEFAULT_SOLVER_PARAMS, SolverParams
from .types import (
    ClipInput,
    ClipPlacement,
    ClockReading,
    ClockSource,
    EdgeKind,
    EdgeReport,
    EdgeStatus,
    Flag,
    ManualCorrections,
    MatchStatus,
    PairwiseMatch,
    PlacementMethod,
    PlacementStatus,
    SyncResult,
)

_DAY_S = 86400.0


@dataclass
class _Edge:
    i: int
    j: int
    #: Audio: offset ``m`` at target time ``time_s``. Clock: the clock reading.
    offset_s: float
    sigma_s: float
    kind: EdgeKind
    confidence: float
    time_s: float = 0.0
    #: Audio only: measured ``r_j - r_i`` and its standard deviation.
    rate: float = 0.0
    rate_sigma: float = float("inf")
    source: ClockSource | None = None
    active: bool = True
    reason: Flag | None = None
    residual_s: float | None = None

    def target(self, rates: np.ndarray) -> float:
        """Right-hand side of ``x_j - x_i = target`` given the current rates."""
        if self.kind != EdgeKind.AUDIO:
            return self.offset_s
        return self.offset_s + rates[self.i] * (self.time_s + self.offset_s) - rates[self.j] * self.time_s


class _OffsetUnionFind:
    """Union-find where each node also stores ``x_node - x_parent``."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.offset = [0.0] * n

    def find(self, i: int) -> tuple[int, float]:
        path = []
        while self.parent[i] != i:
            path.append(i)
            i = self.parent[i]
        root, acc = i, 0.0
        for node in reversed(path):  # compress, accumulating offsets from the root down
            acc += self.offset[node]
            self.parent[node] = root
            self.offset[node] = acc
        return root, (self.offset[path[0]] if path else 0.0)

    def union(self, i: int, j: int, d: float, tol: float) -> bool:
        """Impose ``x_j - x_i = d``. False if it contradicts earlier constraints."""
        ri, oi = self.find(i)
        rj, oj = self.find(j)
        if ri == rj:
            return abs((oj - oi) - d) <= tol
        self.parent[rj] = ri
        self.offset[rj] = oi + d - oj
        return True


def clock_starts(clips: Sequence[ClipInput]) -> dict[tuple[str, ClockReading], float]:
    """Every clip's clock readings, made comparable within each domain.

    Time-of-day readings are unwrapped across midnight, and each domain is
    shifted so its earliest reading is 0 (epoch-based creation times would
    otherwise be ~1.8e9 s, which costs the least-squares solve precision).
    """
    by_domain: dict[str, list[tuple[str, ClockReading]]] = defaultdict(list)
    for c in clips:
        for clock in c.clocks:
            by_domain[clock.domain].append((c.clip_id, clock))
    out: dict[tuple[str, ClockReading], float] = {}
    for members in by_domain.values():
        values = [clock.start_s for _, clock in members]
        if all(clock.source.is_time_of_day for _, clock in members):
            values = unwrap_midnight(values)
        base = min(values)
        out.update({member: v - base for member, v in zip(members, values, strict=True)})
    return out


def clock_sigma(clock: ClockReading, params: SolverParams) -> float:
    if clock.sigma_s is not None:
        return clock.sigma_s
    return {
        ClockSource.TIMECODE: params.timecode_sigma_s,
        ClockSource.BWF: params.bwf_sigma_s,
        ClockSource.CREATION_TIME: params.creation_time_sigma_s,
        ClockSource.CHAPTER: params.chapter_sigma_s,
    }[clock.source]


def unwrap_midnight(values: Sequence[float]) -> list[float]:
    """Unwrap time-of-day readings (seconds) that cross midnight.

    Timecode resets at 24:00:00:00. If readings of one clock span more than 12
    hours, the early-morning ones are taken to belong to the next day.
    """
    vals = list(values)
    if vals and max(vals) - min(vals) > _DAY_S / 2:
        return [v + _DAY_S if v < _DAY_S / 2 else v for v in vals]
    return vals


def audio_edge_sigma(match: PairwiseMatch, params: SolverParams) -> float:
    """Uncertainty of an audio edge's offset, widened for low confidence."""
    base = max(match.estimate.std_error_s, params.min_audio_sigma_s)
    return float(base / max(match.confidence, 0.05))


def solve_placements(
    clips: Sequence[ClipInput],
    matches: Sequence[PairwiseMatch],
    *,
    reference_id: str,
    corrections: ManualCorrections | None = None,
    use_audio: bool = True,
    use_clock: bool = True,
    params: SolverParams = DEFAULT_SOLVER_PARAMS,
) -> SyncResult:
    corrections = corrections or ManualCorrections()
    index = {c.clip_id: k for k, c in enumerate(clips)}
    if reference_id not in index:
        raise ValueError(f"unknown reference clip {reference_id!r}")
    for mo in corrections.offsets:
        if mo.clip_id not in index or mo.anchor_clip_id not in index:
            raise ValueError(f"manual offset references unknown clip: {mo}")
    excluded = set(corrections.excluded_clips)
    durations = [float(c.duration_s or 0.0) for c in clips]
    n_clips = len(clips)
    ref_node = index[reference_id]

    # --- nodes: clips first, then one virtual node per clock domain --------
    domains: dict[str, int] = {}
    if use_clock:
        for c in clips:
            if c.clip_id not in excluded:
                for clock in c.clocks:
                    domains.setdefault(clock.domain, n_clips + len(domains))
    node_names = [c.clip_id for c in clips] + [f"clock:{d}" for d in domains]
    n_nodes = len(node_names)

    # --- soft edges ---------------------------------------------------------
    edges: list[_Edge] = []
    audio_reports: list[tuple[PairwiseMatch, _Edge | None, Flag | None]] = []
    for m in matches:
        reason: Flag | None = None
        if m.ref_id in excluded or m.tgt_id in excluded:
            reason = Flag.EXCLUDED
        elif corrections.is_rejected(m.ref_id, m.tgt_id):
            reason = Flag.USER_REJECTED
        elif not use_audio or m.offset_s is None or m.status == MatchStatus.NO_MATCH:
            reason = Flag.BELOW_THRESHOLD
        elif m.confidence < params.min_edge_confidence:
            reason = Flag.BELOW_THRESHOLD
        if reason is not None:
            audio_reports.append((m, None, reason))
            continue
        est = m.estimate
        e = _Edge(
            i=index[m.ref_id],
            j=index[m.tgt_id],
            offset_s=float(m.offset_s),  # type: ignore[arg-type]
            sigma_s=audio_edge_sigma(m, params),
            kind=EdgeKind.AUDIO,
            confidence=m.confidence,
            time_s=m.offset_time_s,
            rate=-est.drift_ppm * 1e-6,
            rate_sigma=est.drift_std_ppm * 1e-6,
        )
        edges.append(e)
        audio_reports.append((m, e, None))

    if use_clock:
        for (clip_id, clock), start in clock_starts([c for c in clips if c.clip_id not in excluded]).items():
            edges.append(
                _Edge(
                    i=domains[clock.domain],
                    j=index[clip_id],
                    offset_s=start,
                    sigma_s=clock_sigma(clock, params),
                    kind=EdgeKind.CLOCK,
                    confidence=_clock_confidence(clock.source, params),
                    source=clock.source,
                )
            )

    if params.uncertain_edges_bridge_only:
        _drop_redundant_uncertain(edges, corrections, index, n_nodes, params)

    def anchor_for(nodes: list[int]) -> int:
        if ref_node in nodes:
            return ref_node
        clip_nodes = [k for k in nodes if k < n_clips] or nodes
        return max(clip_nodes, key=lambda k: (durations[k] if k < n_clips else -1.0, -k))

    # --- solve with iterative outlier rejection ------------------------------
    # Components are independent, so each iteration rejects the worst edge of every component at once: the same
    # result as rejecting one edge per iteration, in far fewer solves for productions with many sync groups.
    manual_ok: list[bool] = []
    edge_i = np.array([e.i for e in edges], dtype=np.int64)
    edge_j = np.array([e.j for e in edges], dtype=np.int64)
    edge_off = np.array([e.offset_s for e in edges])
    edge_time = np.array([e.time_s for e in edges])
    edge_audio = np.array([e.kind == EdgeKind.AUDIO for e in edges], dtype=bool)
    edge_scale = np.array([max(params.outlier_sigma * e.sigma_s, params.outlier_min_s) for e in edges])
    # Equally inconsistent edges: reject the one touching a clip the user placed by hand (its audio is what the
    # user overrode).
    moved = {index[mo.clip_id] for mo in corrections.offsets}
    edge_moved = np.array([e.i in moved or e.j in moved for e in edges], dtype=bool)
    while True:
        rates = _solve_rates(edges, n_nodes, anchor_for)
        uf, manual_ok = _manual_constraints(corrections, index, rates, durations, n_nodes, params)
        positions, component = _solve_starts(uf, edges, rates, n_nodes, anchor_for)
        if not edges:
            break
        found = [uf.find(k) for k in range(n_nodes)]
        root_of = np.array([r for r, _ in found], dtype=np.int64)
        node_pos = positions[root_of] + np.array([o for _, o in found])
        targets = np.where(
            edge_audio,
            edge_off + rates[edge_i] * (edge_time + edge_off) - rates[edge_j] * edge_time,
            edge_off,
        )
        residuals = node_pos[edge_j] - node_pos[edge_i] - targets
        active = np.array([e.active for e in edges], dtype=bool)
        scores = np.where(active, np.abs(residuals) / edge_scale, 0.0)
        comp_of_edge = np.array([component[r] for r in root_of[edge_i]], dtype=np.int64)
        rank = np.round(scores, 6)
        worst_by_comp: dict[int, int] = {}
        for k in np.flatnonzero(scores > 1.0):
            c = int(comp_of_edge[k])
            w = worst_by_comp.get(c)
            if w is None or (rank[k], edge_moved[k]) > (rank[w], edge_moved[w]):
                worst_by_comp[c] = int(k)
        for k, e in enumerate(edges):
            e.residual_s = float(residuals[k])
        if not worst_by_comp:
            break
        for k in worst_by_comp.values():
            edges[k].active = False
            edges[k].reason = Flag.REJECTED_INCONSISTENT

    warnings = [
        f"manual offset of {mo.clip_id!r} relative to {mo.anchor_clip_id!r} contradicts "
        "earlier manual offsets and was ignored"
        for mo, ok in zip(corrections.offsets, manual_ok, strict=True)
        if not ok
    ]
    manual_clips = {mo.clip_id for mo, ok in zip(corrections.offsets, manual_ok, strict=True) if ok}

    # --- assemble placements ---------------------------------------------------
    def display_start(k: int) -> float:
        return _position(uf, positions, k) + rates[k] * durations[k] / 2.0

    main_comp = component[uf.find(ref_node)[0]]
    comp_clips: dict[int, list[int]] = defaultdict(list)
    for k in range(n_clips):
        if clips[k].clip_id not in excluded:
            comp_clips[component[uf.find(k)[0]]].append(k)
    others = sorted(
        (c for c in comp_clips if c != main_comp and len(comp_clips[c]) > 1),
        key=lambda c: (-sum(durations[k] for k in comp_clips[c]), min(clips[k].clip_id for k in comp_clips[c])),
    )
    group_of = {main_comp: 0, **{c: g + 1 for g, c in enumerate(others)}}
    anchor_of = {main_comp: ref_node}
    for c in others:
        anchor_of[c] = max(comp_clips[c], key=lambda k: (durations[k], clips[k].clip_id))

    incident: dict[int, list[_Edge]] = defaultdict(list)
    for e in edges:
        incident[e.i].append(e)
        incident[e.j].append(e)

    placements: dict[str, ClipPlacement] = {}
    for k, clip in enumerate(clips):
        cid = clip.clip_id
        if cid in excluded:
            placements[cid] = ClipPlacement(
                cid, None, None, PlacementMethod.NONE, 0.0, PlacementStatus.UNSYNCED, (Flag.EXCLUDED,)
            )
            continue
        comp = component[uf.find(k)[0]]
        flags: list[Flag] = []
        mine = incident[k]
        if any(e.kind == EdgeKind.CLOCK and not e.active for e in mine):
            flags.append(Flag.TIMECODE_DISAGREES)
        # A confident audio match the solver had to reject casts doubt on both
        # clips, unless the user placed the other clip by hand (then the user
        # overrode the audio, and only the manual clip carries the note).
        for e in mine:
            if e.kind == EdgeKind.AUDIO and not e.active and e.confidence >= params.confident_threshold:
                other = node_names[e.j if e.i == k else e.i]
                if cid in manual_clips or other not in manual_clips:
                    flags.append(Flag.CONFLICTING_MATCHES)
                    break
        if clip.audio is None:
            flags.append(Flag.NO_AUDIO)

        if comp not in group_of:  # alone in its component
            placements[cid] = ClipPlacement(
                cid, None, None, PlacementMethod.NONE, 0.0, PlacementStatus.UNSYNCED, tuple(flags)
            )
            continue

        active_audio = [e for e in mine if e.active and e.kind == EdgeKind.AUDIO]
        active_clock = [e for e in mine if e.active and e.kind == EdgeKind.CLOCK]
        if k == ref_node:
            method, confidence = PlacementMethod.REFERENCE, 1.0
        elif cid in manual_clips:
            method, confidence = PlacementMethod.MANUAL, 1.0
            flags.append(Flag.MANUAL)
        elif active_audio:
            method, confidence = PlacementMethod.AUDIO, max(e.confidence for e in active_audio)
        elif active_clock:
            best = max(active_clock, key=lambda e: e.confidence)
            method, confidence = _CLOCK_METHOD[best.source], best.confidence  # type: ignore[index]
        else:  # tied to its group only through a manual constraint on another clip
            method, confidence = PlacementMethod.MANUAL, 1.0

        group = group_of[comp]
        if group != 0:
            # Another sync group (a separate recording session): placed within its group, not on the reference's
            # timeline. Only worth a review when the solver is told groups should have been connected.
            flags.append(Flag.DETACHED_GROUP)
        detached_review = group != 0 and params.detached_groups_need_review
        if method == PlacementMethod.MANUAL:
            review = detached_review  # the user's placement is final
        else:
            review = (
                detached_review
                or confidence < params.confident_threshold
                or any(f in flags for f in (Flag.TIMECODE_DISAGREES, Flag.CONFLICTING_MATCHES))
            )
        anchor = anchor_of[comp]
        placements[cid] = ClipPlacement(
            clip_id=cid,
            start_s=display_start(k) - display_start(anchor),
            group=group,
            method=method,
            confidence=confidence,
            status=PlacementStatus.NEEDS_REVIEW if review else PlacementStatus.SYNCED,
            flags=tuple(dict.fromkeys(flags)),
            drift_ppm=_ppm(rates[anchor] - rates[k]),
        )

    # --- edge reports ------------------------------------------------------------
    reports: list[EdgeReport] = [
        EdgeReport(
            kind=EdgeKind.MANUAL,
            node_a=mo.anchor_clip_id,
            node_b=mo.clip_id,
            offset_s=mo.offset_s,
            sigma_s=0.0,
            confidence=1.0,
            status=EdgeStatus.ACCEPTED if ok else EdgeStatus.REJECTED,
            reason=None if ok else Flag.MANUAL_CONFLICT,
        )
        for mo, ok in zip(corrections.offsets, manual_ok, strict=True)
    ]
    for m, e, reason in audio_reports:
        if e is None:
            reports.append(
                EdgeReport(
                    EdgeKind.AUDIO, m.ref_id, m.tgt_id, m.offset_s, None, m.confidence, EdgeStatus.IGNORED, reason
                )
            )
        else:
            reports.append(_edge_report(e, node_names))
    reports.extend(_edge_report(e, node_names) for e in edges if e.kind == EdgeKind.CLOCK)

    return SyncResult(
        reference_id=reference_id,
        placements=placements,
        matches=list(matches),
        edges=reports,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _drop_redundant_uncertain(
    edges: list[_Edge],
    corrections: ManualCorrections,
    index: dict[str, int],
    n_nodes: int,
    params: SolverParams,
) -> None:
    """Deactivate uncertain audio edges inside groups that stronger evidence already connects."""
    parent = list(range(n_nodes))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def strong(e: _Edge) -> bool:
        if e.kind == EdgeKind.AUDIO:
            return e.confidence >= params.confident_threshold
        return e.sigma_s <= params.precise_clock_sigma_s

    for e in edges:
        if strong(e):
            parent[find(e.i)] = find(e.j)
    for mo in corrections.offsets:
        parent[find(index[mo.clip_id])] = find(index[mo.anchor_clip_id])
    for e in edges:
        if e.kind == EdgeKind.AUDIO and not strong(e) and find(e.i) == find(e.j):
            e.active = False
            e.reason = Flag.REDUNDANT_UNCERTAIN


def _solve_rates(edges: list[_Edge], n_nodes: int, anchor_for: Callable[[list[int]], int]) -> np.ndarray:
    """Clock rates relative to each component's anchor, from audio lag slopes."""
    rows = [
        (e.i, e.j, e.rate, 1.0 / e.rate_sigma)
        for e in edges
        if e.active and e.kind == EdgeKind.AUDIO and np.isfinite(e.rate_sigma) and e.rate_sigma > 0
    ]
    values, _ = _graph_lstsq(list(range(n_nodes)), rows, anchor_for)
    return np.array([values[k] for k in range(n_nodes)])


def _manual_constraints(
    corrections: ManualCorrections,
    index: dict[str, int],
    rates: np.ndarray,
    durations: list[float],
    n_nodes: int,
    params: SolverParams,
) -> tuple[_OffsetUnionFind, list[bool]]:
    """Merge manually placed clips. Users set displayed starts ``P = x + r·D/2``."""
    uf = _OffsetUnionFind(n_nodes)
    ok = []
    for mo in corrections.offsets:
        a, c = index[mo.anchor_clip_id], index[mo.clip_id]
        d = mo.offset_s - rates[c] * durations[c] / 2.0 + rates[a] * durations[a] / 2.0
        ok.append(uf.union(a, c, d, params.manual_tolerance_s))
    return uf, ok


def _solve_starts(
    uf: _OffsetUnionFind,
    edges: list[_Edge],
    rates: np.ndarray,
    n_nodes: int,
    anchor_for: Callable[[list[int]], int],
) -> tuple[np.ndarray, dict[int, int]]:
    """Start positions of the union-find roots and the component of every root."""
    roots = sorted({uf.find(k)[0] for k in range(n_nodes)})
    rows = []
    for e in edges:
        if not e.active:
            continue
        ri, oi = uf.find(e.i)
        rj, oj = uf.find(e.j)
        if ri != rj:  # edges inside a manual group only have a residual
            rows.append((ri, rj, e.target(rates) - oj + oi, 1.0 / e.sigma_s))

    members_of: dict[int, list[int]] = defaultdict(list)
    for k in range(n_nodes):
        members_of[uf.find(k)[0]].append(k)

    def root_anchor(comp_roots: list[int]) -> int:
        members = [k for r in comp_roots for k in members_of[r]]
        return uf.find(anchor_for(members))[0]

    values, component = _graph_lstsq(roots, rows, root_anchor)
    positions = np.zeros(n_nodes)
    for r, v in values.items():
        positions[r] = v
    return positions, component


def _graph_lstsq(
    nodes: list[int],
    rows: list[tuple[int, int, float, float]],
    anchor_for: Callable[[list[int]], int],
) -> tuple[dict[int, float], dict[int, int]]:
    """Weighted least squares for ``v_j - v_i ≈ rhs`` (weight w) on a graph.

    Each connected component is solved separately with its anchor fixed at 0.
    Returns the value of every node and a component id (a member node) per node.
    """
    adjacency: dict[int, set[int]] = {n: set() for n in nodes}
    for i, j, _, _ in rows:
        adjacency[i].add(j)
        adjacency[j].add(i)
    component: dict[int, int] = {}
    for n in nodes:
        if n in component:
            continue
        stack, component[n] = [n], n
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if v not in component:
                    component[v] = n
                    stack.append(v)

    members: dict[int, list[int]] = defaultdict(list)
    for n in nodes:
        members[component[n]].append(n)
    comp_rows: dict[int, list[tuple[int, int, float, float]]] = defaultdict(list)
    for row in rows:
        comp_rows[component[row[0]]].append(row)

    values = {n: 0.0 for n in nodes}
    for comp, comp_nodes in members.items():
        if len(comp_nodes) == 1:
            continue
        anchor = anchor_for(comp_nodes)
        col = {n: c for c, n in enumerate(n for n in comp_nodes if n != anchor)}
        x = _weighted_graph_solve(comp_rows[comp], col)
        for n, c in col.items():
            values[n] = float(x[c])
    return values, component


def _weighted_graph_solve(rows: list[tuple[int, int, float, float]], col: dict[int, int]) -> np.ndarray:
    """Least squares for ``v_j - v_i ≈ rhs`` with weights ``w`` (anchor absent from ``col``: fixed at 0).

    Solved through the normal equations, a weighted graph Laplacian with the anchor's row and column removed:
    sparse, symmetric and positive definite for a connected component, so thousands of clips solve in
    milliseconds (a dense least-squares matrix of 20,000 measurements × 4,000 clips would need 640 MB).
    """
    n = len(col)
    ii = np.array([col.get(i, -1) for i, _, _, _ in rows], dtype=np.int64)
    jj = np.array([col.get(j, -1) for _, j, _, _ in rows], dtype=np.int64)
    rhs = np.array([r for _, _, r, _ in rows])
    w2 = np.array([w for _, _, _, w in rows]) ** 2
    diag = np.zeros(n)
    b = np.zeros(n)
    np.add.at(diag, ii[ii >= 0], w2[ii >= 0])
    np.add.at(diag, jj[jj >= 0], w2[jj >= 0])
    np.add.at(b, jj[jj >= 0], (w2 * rhs)[jj >= 0])
    np.subtract.at(b, ii[ii >= 0], (w2 * rhs)[ii >= 0])
    both = (ii >= 0) & (jj >= 0)
    off_r = np.r_[ii[both], jj[both]]
    off_c = np.r_[jj[both], ii[both]]
    off_v = -np.r_[w2[both], w2[both]]
    laplacian = sparse.coo_matrix(
        (np.r_[diag, off_v], (np.r_[np.arange(n), off_r], np.r_[np.arange(n), off_c])), shape=(n, n)
    ).tocsc()
    if n <= 64:
        return np.linalg.solve(laplacian.toarray(), b)
    return np.asarray(spsolve(laplacian, b))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ppm(rate: float) -> float:
    ppm = rate * 1e6
    return 0.0 if abs(ppm) < 1e-9 else ppm


def _clock_confidence(source: ClockSource, params: SolverParams) -> float:
    return {
        ClockSource.TIMECODE: params.timecode_confidence,
        ClockSource.BWF: params.timecode_confidence,
        ClockSource.CREATION_TIME: params.creation_time_confidence,
        ClockSource.CHAPTER: params.chapter_confidence,
    }[source]


_CLOCK_METHOD = {
    ClockSource.TIMECODE: PlacementMethod.TIMECODE,
    ClockSource.BWF: PlacementMethod.TIMECODE,
    ClockSource.CREATION_TIME: PlacementMethod.METADATA,
    ClockSource.CHAPTER: PlacementMethod.CHAPTER,
}


def _edge_report(e: _Edge, names: list[str]) -> EdgeReport:
    return EdgeReport(
        kind=e.kind,
        node_a=names[e.i],
        node_b=names[e.j],
        offset_s=e.offset_s,
        sigma_s=e.sigma_s,
        confidence=e.confidence,
        status=EdgeStatus.ACCEPTED if e.active else EdgeStatus.REJECTED,
        reason=e.reason,
        residual_s=e.residual_s,
    )


def _position(uf: _OffsetUnionFind, positions: np.ndarray, node: int) -> float:
    root, off = uf.find(node)
    return float(positions[root] + off)
