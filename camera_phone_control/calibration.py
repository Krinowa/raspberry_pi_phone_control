"""Four camera corners -> normalized Android display, using a homography."""

import math

import numpy as np


def unit_point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("A point must contain x and y.")

    if any(
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or not 0 <= v <= 1
        for v in value
    ):
        raise ValueError("Coordinates must be finite numbers between 0 and 1.")
    return np.array(value, dtype=float)


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


class Calibration:
    def __init__(self, corners):
        if not isinstance(corners, list) or len(corners) != 4:
            raise ValueError(
                "Select four corners: top-left, top-right, bottom-right, bottom-left."
            )

        self.corners = np.array([unit_point(p) for p in corners])

        turns = [
            cross(
                self.corners[(i + 1) % 4] - self.corners[i],
                self.corners[(i + 2) % 4] - self.corners[(i + 1) % 4],
            )
            for i in range(4)
        ]

        if not (all(t > 1e-6 for t in turns) or all(t < -1e-6 for t in turns)):
            raise ValueError(
                "Corners must form a convex, non-crossing display outline."
            )

        area = (
            abs(
                sum(cross(self.corners[i], self.corners[(i + 1) % 4]) for i in range(4))
            )
            / 2
        )

        if area < 0.005:
            raise ValueError(
                "Selected display is too small. Move the camera closer and recalibrate."
            )

        rows, values = [], []
        for (x, y), (u, v) in zip(self.corners, [(0, 0), (1, 0), (1, 1), (0, 1)]):
            rows.extend(
                ([x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y])
            )
            values.extend((u, v))

        try:
            matrix = np.array(rows)
            if np.linalg.cond(matrix) > 1e10:
                raise ValueError(
                    "Calibration is unstable. Select a clearer view of the four corners."
                )
            self.transform = np.append(np.linalg.solve(matrix, values), 1).reshape(3, 3)

        except np.linalg.LinAlgError as exc:
            raise ValueError(
                "Cannot calculate calibration from these corners."
            ) from exc

    def map(self, point):
        p = unit_point(point)

        sides = [
            cross(self.corners[(i + 1) % 4] - self.corners[i], p - self.corners[i])
            for i in range(4)
        ]

        if not (all(s >= -1e-9 for s in sides) or all(s <= 1e-9 for s in sides)):
            raise ValueError("Click is outside the calibrated phone display.")

        result = self.transform @ [p[0], p[1], 1]

        if abs(result[2]) < 1e-10:
            raise ValueError("Click cannot be mapped with this calibration.")
        normalized = result[:2] / result[2]

        if (
            not np.isfinite(normalized).all()
            or (normalized < -1e-6).any()
            or (normalized > 1 + 1e-6).any()
        ):
            raise ValueError("Mapped click is outside the phone display.")
        return np.clip(normalized, 0, 1).tolist()

    def pixel(self, point, width, height):
        u, v = self.map(point)
        # Nearest pixel; a tiny tolerance prevents solver noise changing half-pixels.
        return (
            math.floor(u * (width - 1) + 0.5 + 1e-9),
            math.floor(v * (height - 1) + 0.5 + 1e-9),
        ), (u, v)
