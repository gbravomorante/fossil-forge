"""Smooth generated boundary rings without moving their measured attachment."""

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .geometry import GeometryError


def boundary_loops(boundary):
    successor = {int(a): int(b) for a, b in boundary}
    pending = set(successor)
    loops = []
    while pending:
        start = min(pending)
        current = start
        loop = []
        while current in pending:
            pending.remove(current)
            loop.append(current)
            current = successor[current]
        if current != start:
            raise GeometryError("The cut boundary does not form separate closed loops.")
        loops.append(np.array(loop, dtype=int))
    return loops


def smooth_loop(points, width):
    if width <= 0:
        return points.copy()
    lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
    distance = np.r_[0, np.cumsum(lengths)]
    count = max(128, min(8192, int(np.ceil(distance[-1] / min(0.05, width / 4)))))
    uniform = np.linspace(0, distance[-1], count, endpoint=False)
    closed = np.vstack((points, points[0]))
    sampled = np.column_stack([np.interp(uniform, distance, closed[:, j]) for j in range(3)])
    smooth = gaussian_filter1d(sampled, width / (distance[-1] / count), axis=0, mode="wrap")
    return np.column_stack(
        [
            np.interp(
                distance[:-1], np.r_[uniform, distance[-1]], np.r_[smooth[:, j], smooth[0, j]]
            )
            for j in range(3)
        ]
    )


def connect_skins(vertices, boundary, back_offset, width):
    """Use several smooth rings so edge corrugations fade near each skin."""
    a, b = boundary.T

    def backing_ids(ids):
        return ids + back_offset if np.isscalar(back_offset) else back_offset[ids]

    if width <= 0:
        return vertices, np.vstack(
            (
                np.column_stack((b, a, backing_ids(a))),
                np.column_stack((b, backing_ids(a), backing_ids(b))),
            )
        )
    loops = boundary_loops(boundary)
    extra = []
    walls = []
    next_vertex = len(vertices)
    for loop in loops:
        original = vertices[loop]
        back = vertices[backing_ids(loop)]
        smooth_original = smooth_loop(original, width)
        smooth_back = smooth_loop(back, width)
        previous = loop
        for t in np.linspace(0, 1, 17)[1:]:
            if t == 1:
                current = backing_ids(loop)
            else:
                blend = min(1.0, t * 8, (1 - t) * 8)
                blend = blend * blend * (3 - 2 * blend)
                raw = (1 - t) * original + t * back
                smooth = (1 - t) * smooth_original + t * smooth_back
                extra.append(raw + blend * (smooth - raw))
                current = np.arange(next_vertex, next_vertex + len(loop))
                next_vertex += len(loop)
            aa = previous
            bb = np.roll(previous, -1)
            cc = current
            dd = np.roll(current, -1)
            walls.extend((np.column_stack((bb, aa, cc)), np.column_stack((bb, cc, dd))))
            previous = current
    return np.vstack([vertices, *extra]), np.vstack(walls)


def triangulate_outline(points, direction):
    """Ear clipping of a planar simple outline; fail visibly on folded loops."""
    axis = np.zeros(3)
    axis[np.argmin(np.abs(direction))] = 1
    u = np.cross(axis, direction)
    u /= np.linalg.norm(u)
    v = np.cross(direction, u)
    xy = np.column_stack((points @ u, points @ v))

    def cross(a, b):
        return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]

    area = float(cross(xy, np.roll(xy, -1, axis=0)).sum())
    if abs(area) < 1e-10:
        raise GeometryError("The flat back has no usable area along this arrow.")
    orientation = 1 if area > 0 else -1
    pending = list(range(len(points)))
    faces = []
    while len(pending) > 3:
        remaining = np.array(pending)
        prev = np.roll(remaining, 1)
        following = np.roll(remaining, -1)
        sizes = orientation * cross(xy[remaining] - xy[prev], xy[following] - xy[remaining])
        candidates = np.argsort(-sizes)
        found = False
        for position in candidates:
            if sizes[position] <= 1e-12:
                break
            a, b, c = prev[position], remaining[position], following[position]
            others = remaining[(remaining != a) & (remaining != b) & (remaining != c)]
            q = xy[others]
            inside = (
                (orientation * cross(xy[b] - xy[a], q - xy[a]) >= -1e-12)
                & (orientation * cross(xy[c] - xy[b], q - xy[b]) >= -1e-12)
                & (orientation * cross(xy[a] - xy[c], q - xy[c]) >= -1e-12)
            )
            if inside.any():
                continue
            faces.append((a, b, c))
            pending.pop(int(position))
            found = True
            break
        if not found:
            raise GeometryError(
                "The projected cut edge folds over itself. Increase bridge smoothing or use the mirrored back."
            )
    faces.append(tuple(pending))
    faces = np.array(faces, dtype=int)
    # Reverse the original loop order to match the opposite skin and walls.
    # The completed shell is oriented outward after its volume is measured.
    return faces[:, ::-1]


def flat_back(source, boundary, center, direction, scale, gap, width):
    loops = boundary_loops(boundary)
    if len(loops) != 1:
        raise GeometryError(
            "Flat back currently needs one cut-boundary loop. Use the mirrored skin for fragments with interior holes or separate outlines."
        )
    loop = loops[0]
    original = source.vertices[loop]
    lengths = np.linalg.norm(np.roll(original, -1, axis=0) - original, axis=1)
    distance = np.r_[0, np.cumsum(lengths)]
    parameter = distance[:-1] / distance[-1]
    # A regular, coarser cap avoids long nearly collapsed triangles created
    # by carrying thousands of unevenly spaced scan vertices into one plane.
    count = max(32, min(1024, int(np.ceil(distance[-1] / 0.15))))
    uniform = np.arange(count) / count
    closed = np.vstack((original, original[0]))
    sampled = np.column_stack(
        [np.interp(uniform, np.r_[parameter, 1], closed[:, j]) for j in range(3)]
    )
    relative = sampled - center
    projected = relative - (relative @ direction)[:, None] * direction
    projected = smooth_loop(projected, max(width, 0.1))
    level = float(((source.vertices - center) @ direction).max() + gap)
    backing = center + scale * projected + level * direction
    back_faces = triangulate_outline(backing, direction) + len(source.vertices)
    vertices = [source.vertices, backing]
    next_vertex = len(source.vertices) + count
    back_ids = np.arange(len(source.vertices), len(source.vertices) + count)
    previous = loop
    previous_parameter = parameter
    walls = []
    smooth_original = smooth_loop(sampled, width)
    for t in np.linspace(0, 1, 17)[1:]:
        if t == 1:
            current = back_ids
        else:
            blend = min(1.0, t * 8)
            blend = blend * blend * (3 - 2 * blend)
            start = sampled + blend * (smooth_original - sampled)
            vertices.append((1 - t) * start + t * backing)
            current = np.arange(next_vertex, next_vertex + count)
            next_vertex += count
        walls.extend(zipper_faces(previous, previous_parameter, current, uniform))
        previous = current
        previous_parameter = uniform
    vertices = np.vstack(vertices)
    walls = np.array(walls, dtype=int)
    return vertices, back_faces, walls, backing, level


def zipper_faces(first, first_parameter, second, second_parameter):
    """Join closed contours with different vertex counts in matching order."""
    i = j = 0
    faces = []
    n = len(first)
    m = len(second)
    while i < n or j < m:
        next_first = first_parameter[i + 1] if i + 1 < n else 1.0
        next_second = second_parameter[j + 1] if j + 1 < m else 1.0
        if i < n and (j == m or next_first <= next_second):
            faces.append((first[(i + 1) % n], first[i % n], second[j % m]))
            i += 1
        else:
            faces.append((first[i % n], second[j % m], second[(j + 1) % m]))
            j += 1
    return faces
