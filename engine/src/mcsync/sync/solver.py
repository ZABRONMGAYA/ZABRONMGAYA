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
from scipy.sparse.csgraph import connected_components
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
        _drop_redundant_uncertain(edges, corrections, index, n_nodes, [c.device_id for c in clips], params)

    def anchor_for(nodes: list[int]) -> int:
        if ref_node in nodes:
            return ref_node
        clip_nodes = [k for k in nodes if k < n_clips] or nodes
        return max(clip_nodes, key=lambda k: (durations[k] if k < n_clips else -1.0, -k))

    # --- solve with iterative outlier rejection ------------------------------
    # Components are independent, so each iteration rejects the worst edge of every component at once: the same
    # result as rejecting one edge per iteration, in far fewer solves for productions with many sync groups. The
    # loop works on arrays (one sparse solve for all components) so a 4,000-clip production solves in seconds.
    manual_ok: list[bool] = []
    ea = _EdgeArrays(edges, params)
    # Equally inconsistent edges: reject the one touching a clip the user placed by hand (its audio is what the
    # user overrode).
    moved = {index[mo.clip_id] for mo in corrections.offsets}
    edge_moved = np.array([e.i in moved or e.j in moved for e in edges], dtype=bool)
    residuals = np.zeros(len(edges))
    while True:
        rates = _solve_rates(ea, n_nodes, anchor_for)
        uf, manual_ok = _manual_constraints(corrections, index, rates, durations, n_nodes, params)
        root_of, off_of = _flatten(uf, n_nodes)
        targets = ea.targets(rates)
        positions, component, comp_of_root = _solve_starts(ea, targets, root_of, off_of, n_nodes, uf, anchor_for)
        if not edges:
            break
        node_pos = positions[root_of] + off_of
        residuals = node_pos[ea.j] - node_pos[ea.i] - targets
        scores = np.where(ea.active, np.abs(residuals) / ea.scale, 0.0)
        bad = np.flatnonzero(scores > 1.0)
        if len(bad) == 0:
            break
        # The worst edge of every component (ties: the one touching a clip placed by hand).
        comp = comp_of_root[root_of[ea.i[bad]]]
        order = np.lexsort((edge_moved[bad], np.round(scores[bad], 6), comp))
        last_of_comp = np.r_[comp[order][1:] != comp[order][:-1], True]
        ea.active[bad[order[last_of_comp]]] = False
    for k, e in enumerate(edges):
        e.residual_s = float(residuals[k])
        if e.active and not ea.active[k]:
            e.active = False
            e.reason = Flag.REJECTED_INCONSISTENT

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
    devices: list[str | None],
    params: SolverParams,
) -> None:
    """Keep uncertain audio edges only where they attach something that stronger evidence leaves unconnected.

    Inside a group that confident matches, precise clocks or manual offsets already connect, an uncertain edge is
    redundant. Between groups, an uncertain match may attach the clips of one device (a camera whose audio is poor)
    to a group, but not merge two groups that each hold several devices: joining whole sessions on uncertain
    evidence is how unrelated sessions end up on one timeline. Links between groups are taken strongest first, and
    a group that one link has grown counts as its merged self for the next.
    """
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

    # Devices per group (clip nodes only; a clip without a device counts as its own).
    kinds: dict[int, set[str]] = defaultdict(set)
    for k, dev in enumerate(devices):
        kinds[find(k)].add(dev if dev is not None else f"clip:{k}")
    links: dict[tuple[int, int], list[_Edge]] = defaultdict(list)
    for e in edges:
        if e.kind != EdgeKind.AUDIO or strong(e):
            continue
        a, b = find(e.i), find(e.j)
        if a == b:
            e.active = False
            e.reason = Flag.REDUNDANT_UNCERTAIN
        else:
            links[(min(a, b), max(a, b))].append(e)
    for (a, b), group in sorted(links.items(), key=lambda kv: -sum(e.confidence for e in kv[1])):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue  # joined through another link: these edges still vote on the placement
        if len(kinds[ra]) > 1 and len(kinds[rb]) > 1:
            for e in group:
                e.active = False
                e.reason = Flag.UNCERTAIN_MERGE
            continue
        parent[ra] = rb
        kinds[rb] |= kinds.pop(ra)


class _EdgeArrays:
    """The numeric side of the edges, as arrays, for the solve loop."""

    def __init__(self, edges: list[_Edge], params: SolverParams) -> None:
        self.i = np.array([e.i for e in edges], dtype=np.int64)
        self.j = np.array([e.j for e in edges], dtype=np.int64)
        self.offset = np.array([e.offset_s for e in edges], dtype=np.float64)
        self.time = np.array([e.time_s for e in edges], dtype=np.float64)
        self.audio = np.array([e.kind == EdgeKind.AUDIO for e in edges], dtype=bool)
        self.sigma = np.array([e.sigma_s for e in edges], dtype=np.float64)
        self.rate = np.array([e.rate for e in edges], dtype=np.float64)
        self.rate_sigma = np.array([e.rate_sigma for e in edges], dtype=np.float64)
        self.scale = np.maximum(params.outlier_sigma * self.sigma, params.outlier_min_s)
        self.active = np.array([e.active for e in edges], dtype=bool)

    def targets(self, rates: np.ndarray) -> np.ndarray:
        """Right-hand side of ``x_j - x_i = target`` for every edge (see :meth:`_Edge.target`)."""
        if len(self.i) == 0:
            return np.zeros(0)
        audio = self.offset + rates[self.i] * (self.time + self.offset) - rates[self.j] * self.time
        return np.where(self.audio, audio, self.offset)


def _solve_rates(ea: _EdgeArrays, n_nodes: int, anchor_for: Callable[[list[int]], int]) -> np.ndarray:
    """Clock rates relative to each component's anchor, from audio lag slopes."""
    with np.errstate(divide="ignore", invalid="ignore"):
        m = ea.active & ea.audio & np.isfinite(ea.rate_sigma) & (ea.rate_sigma > 0)
        values, _ = _graph_lstsq(n_nodes, ea.i[m], ea.j[m], ea.rate[m], 1.0 / ea.rate_sigma[m], anchor_for)
    return values


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


def _flatten(uf: _OffsetUnionFind, n_nodes: int) -> tuple[np.ndarray, np.ndarray]:
    """Root and offset from the root of every node (only manually merged nodes differ from themselves)."""
    root_of = np.arange(n_nodes, dtype=np.int64)
    off_of = np.zeros(n_nodes)
    for k in range(n_nodes):
        if uf.parent[k] != k:
            root_of[k], off_of[k] = uf.find(k)
    return root_of, off_of


def _solve_starts(
    ea: _EdgeArrays,
    targets: np.ndarray,
    root_of: np.ndarray,
    off_of: np.ndarray,
    n_nodes: int,
    uf: _OffsetUnionFind,
    anchor_for: Callable[[list[int]], int],
) -> tuple[np.ndarray, dict[int, int], np.ndarray]:
    """Start positions of the union-find roots, the component of every root (as a dict and as an array)."""
    ri, rj = root_of[ea.i], root_of[ea.j]
    m = ea.active & (ri != rj)  # edges inside a manual group only have a residual
    rhs = targets - off_of[ea.j] + off_of[ea.i]
    order = np.argsort(root_of, kind="stable")
    starts = np.searchsorted(root_of[order], np.arange(n_nodes + 1))

    def root_anchor(comp_roots: list[int]) -> int:
        members = [int(k) for r in comp_roots for k in order[starts[r] : starts[r + 1]]]
        return uf.find(anchor_for(members))[0]

    positions, comp_of_node = _graph_lstsq(n_nodes, ri[m], rj[m], rhs[m], 1.0 / ea.sigma[m], root_anchor)
    roots = np.unique(root_of)
    return positions, {int(r): int(comp_of_node[r]) for r in roots}, comp_of_node


def _graph_lstsq(
    n_nodes: int,
    i: np.ndarray,
    j: np.ndarray,
    rhs: np.ndarray,
    w: np.ndarray,
    anchor_for: Callable[[list[int]], int],
) -> tuple[np.ndarray, np.ndarray]:
    """Weighted least squares for ``v_j - v_i ≈ rhs`` (weight ``w``) on a graph of ``n_nodes`` nodes.

    Every connected component is anchored (its anchor fixed at 0) and all of them are solved at once through the
    normal equations: a weighted graph Laplacian with the anchors' rows and columns removed, sparse, symmetric and
    positive definite (a dense least-squares matrix of 20,000 measurements × 4,000 clips would need 640 MB).
    Returns every node's value and its component id (the smallest node of the component).
    """
    graph = sparse.coo_matrix((np.ones(len(i)), (i, j)), shape=(n_nodes, n_nodes))
    _, labels = connected_components(graph, directed=False)
    order = np.argsort(labels, kind="stable")
    bounds = np.r_[0, np.flatnonzero(np.diff(labels[order])) + 1, n_nodes]
    comp_id = np.empty(n_nodes, dtype=np.int64)
    is_anchor = np.zeros(n_nodes, dtype=bool)
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        members = order[a:b]
        comp_id[members] = members.min()
        if b - a > 1:
            is_anchor[anchor_for([int(k) for k in members])] = True
    sizes = np.bincount(labels)
    unknown = (sizes[labels] > 1) & ~is_anchor
    col = np.full(n_nodes, -1, dtype=np.int64)
    col[unknown] = np.arange(int(unknown.sum()))
    values = np.zeros(n_nodes)
    n = int(unknown.sum())
    if n == 0 or len(i) == 0:
        return values, comp_id
    ii, jj = col[i], col[j]
    w2 = np.asarray(w, dtype=np.float64) ** 2
    diag = np.zeros(n)
    b = np.zeros(n)
    np.add.at(diag, ii[ii >= 0], w2[ii >= 0])
    np.add.at(diag, jj[jj >= 0], w2[jj >= 0])
    np.add.at(b, jj[jj >= 0], (w2 * rhs)[jj >= 0])
    np.subtract.at(b, ii[ii >= 0], (w2 * rhs)[ii >= 0])
    both = (ii >= 0) & (jj >= 0)
    rows = np.r_[np.arange(n), ii[both], jj[both]]
    cols = np.r_[np.arange(n), jj[both], ii[both]]
    vals = np.r_[diag, -w2[both], -w2[both]]
    laplacian = sparse.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsc()
    x = np.linalg.solve(laplacian.toarray(), b) if n <= 64 else np.asarray(spsolve(laplacian, b))
    values[unknown] = x
    return values, comp_id


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
