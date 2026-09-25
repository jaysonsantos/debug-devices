"""Live tracking: follow the board in the phone screen stream after a photo registration.

At registration, the tracker matches the snapshot with the current screen frame (ORB features + RANSAC). That gives
`snapshot_to_frame`: agent snapshot pixels -> frame pixels. Then each new frame is matched with the reference frame
(or, when the view moved far, with the last good frame): `motion` = reference frame -> current frame. The board
mm -> current snapshot pixels map is then

    inv(snapshot_to_frame) @ motion @ snapshot_to_frame @ board_to_snapshot

The snapshot and the preview crop the sensor around the same center, so this also follows a zoom change.
"""

import io
from dataclasses import dataclass, field
from enum import StrEnum

import cv2
import numpy as np
from PIL import Image as PilImage

# Frames and snapshots are matched at this width (grayscale).
TRACK_WIDTH = 720
ORB_FEATURES = 3000
# Lowe's ratio test: the best match must be clearly better than the second best.
MATCH_RATIO = 0.75
# The best and the second best match of each feature.
NEIGHBORS = 2
# MAGSAC++ (more exact than plain RANSAC in the tests), with this inlier threshold in tracking pixels.
RANSAC_REPROJECTION_PX = 3.0
# A homography is trusted with at least this many inliers, and this part of the ratio-test matches.
MIN_INLIERS = 25
MIN_INLIER_RATIO = 0.3
# A plausible camera motion: the scale of the map stays in this range, and it does not mirror the image.
MIN_SCALE = 0.25
MAX_SCALE = 4.0
# The perspective terms of a board seen from a normal camera angle stay far below this (1/px).
MAX_PERSPECTIVE = 1e-2
# This many failed frames in a row: the tracking is lost.
LOST_FRAMES = 3
# The camera preview part of the screen frame (fractions): the app status text at the top left stays out.
PREVIEW_LEFT = 0.03
PREVIEW_TOP = 0.12
PREVIEW_RIGHT = 0.97
PREVIEW_BOTTOM = 0.97

type Matrix = np.ndarray


class TrackingState(StrEnum):
    OFF = "off"
    FOLLOWING = "following"
    LOST = "lost"


@dataclass
class Features:
    image: np.ndarray
    keypoints: tuple
    descriptors: np.ndarray | None


@dataclass
class MatchResult:
    matrix: Matrix | None
    inliers: int
    matches: int

    @property
    def ok(self) -> bool:
        return self.matrix is not None


def gray(jpeg: bytes, width: int = TRACK_WIDTH) -> tuple[np.ndarray, float]:
    """A grayscale image `width` wide, and the scale from the original pixels to it."""
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        opened.draft("L", (width, width * 4))
        image = opened.convert("L")
        original_width = opened.size[0]
    scale = width / image.width
    height = max(1, round(image.height * scale))
    resized = image.resize((width, height))
    # The draft decode can make the image smaller than the original: the scale is from the original pixels.
    return np.asarray(resized, dtype=np.uint8), width / original_width


def preview_mask(shape: tuple[int, int]) -> np.ndarray:
    height, width = shape
    mask = np.zeros(shape, dtype=np.uint8)
    top, bottom = round(height * PREVIEW_TOP), round(height * PREVIEW_BOTTOM)
    left, right = round(width * PREVIEW_LEFT), round(width * PREVIEW_RIGHT)
    mask[top:bottom, left:right] = 255
    return mask


def features(image: np.ndarray, mask: np.ndarray | None = None) -> Features:
    orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
    keypoints, descriptors = orb.detectAndCompute(image, mask)
    return Features(image=image, keypoints=keypoints, descriptors=descriptors)


def plausible(matrix: Matrix) -> bool:
    """No mirror, no collapse, no wild perspective."""
    linear = matrix[:2, :2] / matrix[2, 2]
    determinant = float(np.linalg.det(linear))
    if determinant <= 0:
        return False
    scale = determinant**0.5
    perspective = float(np.abs(matrix[2, :2] / matrix[2, 2]).max())
    return MIN_SCALE <= scale <= MAX_SCALE and perspective < MAX_PERSPECTIVE


def match(source: Features, target: Features) -> MatchResult:
    """The homography source pixels -> target pixels, or no matrix when it is not trusted."""
    if source.descriptors is None or target.descriptors is None or len(target.keypoints) < MIN_INLIERS:
        return MatchResult(matrix=None, inliers=0, matches=0)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(source.descriptors, target.descriptors, k=NEIGHBORS)
    good = [pair[0] for pair in pairs if len(pair) == NEIGHBORS and pair[0].distance < MATCH_RATIO * pair[1].distance]
    if len(good) < MIN_INLIERS:
        return MatchResult(matrix=None, inliers=0, matches=len(good))
    src = np.float32([source.keypoints[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([target.keypoints[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    matrix, inlier_mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, RANSAC_REPROJECTION_PX)
    inliers = int(inlier_mask.sum()) if inlier_mask is not None else 0
    trusted = (
        matrix is not None and inliers >= MIN_INLIERS and inliers >= MIN_INLIER_RATIO * len(good) and plausible(matrix)
    )
    return MatchResult(matrix=matrix if trusted else None, inliers=inliers, matches=len(good))


def scale_matrix(factor: float) -> Matrix:
    return np.diag([factor, factor, 1.0])


def apply(matrix: Matrix, points: np.ndarray) -> np.ndarray:
    """Map (n, 2) points through a 3x3 homography."""
    homogeneous = np.hstack([points, np.ones((len(points), 1))]) @ matrix.T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


@dataclass
class LiveTracker:
    """Follows one registration. `board_to_snapshot`: board mm -> pixels of the agent's snapshot at registration."""

    registration_id: str
    board_to_snapshot: Matrix
    snapshot_to_frame: Matrix
    reference: Features
    motion: Matrix = field(default_factory=lambda: np.eye(3))
    last: Features | None = None
    last_motion: Matrix = field(default_factory=lambda: np.eye(3))
    misses: int = 0
    state: TrackingState = TrackingState.FOLLOWING
    inliers: int = 0

    @classmethod
    def start(
        cls, registration_id: str, board_to_snapshot: Matrix, snapshot_jpeg: bytes, frame_jpeg: bytes
    ) -> LiveTracker | str:
        """A tracker, or why the snapshot does not match the screen frame."""
        snapshot, snapshot_scale = gray(snapshot_jpeg)
        frame, _ = gray(frame_jpeg)
        frame_features = features(frame, preview_mask(frame.shape))
        found = match(features(snapshot), frame_features)
        if not found.ok:
            return f"the snapshot does not match the live screen ({found.inliers} of {MIN_INLIERS} feature matches)"
        assert found.matrix is not None
        # Snapshot pixels -> tracking snapshot pixels -> tracking frame pixels. The frames stay at tracking size.
        snapshot_to_frame = found.matrix @ scale_matrix(snapshot_scale)
        return cls(
            registration_id=registration_id,
            board_to_snapshot=board_to_snapshot,
            snapshot_to_frame=snapshot_to_frame,
            reference=frame_features,
            inliers=found.inliers,
        )

    @property
    def following(self) -> bool:
        return self.state is TrackingState.FOLLOWING

    def update(self, frame_jpeg: bytes) -> bool:
        """Match one new frame. Returns True when the tracking was lost with this frame."""
        if not self.following:
            return False
        frame, _ = gray(frame_jpeg)
        current = features(frame, preview_mask(frame.shape))
        found = match(self.reference, current) if frame.shape == self.reference.image.shape else None
        motion = found.matrix if found is not None and found.ok else None
        if motion is None and self.last is not None and frame.shape == self.last.image.shape:
            # Far from the reference: chain through the last good frame.
            step = match(self.last, current)
            if step.ok:
                assert step.matrix is not None
                motion, found = step.matrix @ self.last_motion, step
        if motion is None:
            self.misses += 1
            if self.misses >= LOST_FRAMES:
                self.state = TrackingState.LOST
                return True
            return False
        self.misses = 0
        self.motion = motion
        self.last, self.last_motion = current, motion
        self.inliers = found.inliers if found is not None else 0
        return False

    def board_to_current(self) -> Matrix:
        """Board mm -> pixels of a snapshot taken now (same size and flips as the agent's snapshot)."""
        return np.linalg.inv(self.snapshot_to_frame) @ self.motion @ self.snapshot_to_frame @ self.board_to_snapshot
