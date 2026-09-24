"""Map board positions (mm) to photo pixels with a homography, fitted from point pairs (DLT with SVD)."""

import numpy as np
from pydantic import BaseModel

MIN_PAIRS = 4
# With this many pairs or more, the fit has spare pairs, so the reprojection error means something.
MIN_PAIRS_FOR_CHECK = 5
# Finding the one wrong pair needs a spare pair after the check: 4 exact + 1 check + the suspect.
MIN_PAIRS_FOR_OUTLIER = MIN_PAIRS_FOR_CHECK + 1
# Refuse a fit whose largest reprojection error is above this part of the photo point spread (diagonal).
MAX_ERROR_FRACTION = 0.02
HOMOGENEOUS_COORDS = 3


class HomographyError(Exception):
    """The pairs cannot give a usable mapping."""


class PairError(BaseModel):
    board_x_mm: float
    board_y_mm: float
    photo_x_px: float
    photo_y_px: float
    error_px: float


class Fit(BaseModel):
    matrix: list[list[float]]
    rms_error_px: float
    max_error_px: float
    # The limit that max_error_px must not pass (px). None when there are too few pairs for a check.
    error_limit_px: float | None
    pairs: list[PairError]


def _normalize(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hartley normalization: mean 0, mean distance sqrt(2). Returns the points and the transform."""
    mean = points.mean(axis=0)
    distance = np.linalg.norm(points - mean, axis=1).mean()
    if distance == 0:
        raise HomographyError("all points are at the same position")
    scale = np.sqrt(2) / distance
    transform = np.array([[scale, 0, -scale * mean[0]], [0, scale, -scale * mean[1]], [0, 0, 1]])
    ones = np.ones((len(points), 1))
    return (transform @ np.hstack([points, ones]).T).T[:, :2], transform


def apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    ones = np.ones((len(points), 1))
    mapped = (matrix @ np.hstack([points, ones]).T).T
    return mapped[:, :2] / mapped[:, 2:3]


def fit_homography(board_mm: list[tuple[float, float]], photo_px: list[tuple[float, float]]) -> Fit:
    if len(board_mm) != len(photo_px):
        raise HomographyError("board and photo point lists have different lengths")
    if len(board_mm) < MIN_PAIRS:
        raise HomographyError(f"a homography needs at least {MIN_PAIRS} pairs, not {len(board_mm)}")
    source = np.asarray(board_mm, dtype=float)
    target = np.asarray(photo_px, dtype=float)
    source_n, source_t = _normalize(source)
    target_n, target_t = _normalize(target)
    rows = []
    for (x, y), (u, v) in zip(source_n, target_n, strict=True):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, singular, vt = np.linalg.svd(np.asarray(rows))
    if singular[-2] < np.finfo(float).eps * singular[0]:
        raise HomographyError("the pairs are degenerate (for example 3 of 4 points on one line)")
    normalized = vt[-1].reshape(HOMOGENEOUS_COORDS, HOMOGENEOUS_COORDS)
    matrix = np.linalg.inv(target_t) @ normalized @ source_t
    matrix /= matrix[2, 2]
    mapped = apply(matrix, source)
    errors = np.linalg.norm(mapped - target, axis=1)
    spread = float(np.linalg.norm(target.max(axis=0) - target.min(axis=0)))
    limit = MAX_ERROR_FRACTION * spread if len(board_mm) >= MIN_PAIRS_FOR_CHECK else None
    return Fit(
        matrix=matrix.tolist(),
        rms_error_px=float(np.sqrt((errors**2).mean())),
        max_error_px=float(errors.max()),
        error_limit_px=limit,
        pairs=[
            PairError(board_x_mm=bx, board_y_mm=by, photo_x_px=px, photo_y_px=py, error_px=float(error))
            for (bx, by), (px, py), error in zip(board_mm, photo_px, errors, strict=True)
        ],
    )


def map_points(matrix: list[list[float]], points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return []
    mapped = apply(np.asarray(matrix), np.asarray(points, dtype=float))
    return [(float(x), float(y)) for x, y in mapped]


def likely_outlier(board_mm: list[tuple[float, float]], photo_px: list[tuple[float, float]]) -> int | None:
    """The index of the pair that is most likely wrong, or None when the pairs cannot tell.

    For each pair, fit the other pairs and take their largest error. When the other pairs agree (a small error)
    without this pair, this pair is the suspect. With 5 pairs, any 4 pairs fit exactly, so it needs 6 or more.
    """
    if len(board_mm) < MIN_PAIRS_FOR_OUTLIER:
        return None
    rest_errors = []
    for index in range(len(board_mm)):
        others = [position for position in range(len(board_mm)) if position != index]
        rest_errors.append(fit_homography([board_mm[i] for i in others], [photo_px[i] for i in others]).max_error_px)
    best = min(range(len(rest_errors)), key=rest_errors.__getitem__)
    spread = float(np.linalg.norm(np.ptp(np.asarray(photo_px, dtype=float), axis=0)))
    return best if rest_errors[best] <= MAX_ERROR_FRACTION * spread else None
