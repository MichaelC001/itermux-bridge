"""Composite a whole iTerm2 tab (all its split panes) into one screen.

Attaching to a *window* means seeing every pane at once, with dividers between
them — what tmux does. iTerm2 hands us the split tree (`tab.root`): nested
Splitters, each either vertical (children side by side) or horizontal (children
stacked), with Sessions at the leaves.

We walk that tree, give every pane a rectangle in the client's grid in proportion
to its real size, and draw a divider line between siblings.

    Splitter(vertical=True)         ->  columns
      Splitter(vertical=False)      ->  rows within the left column
        Session 99x16
        ...
"""

from typing import List, NamedTuple


class Region(NamedTuple):
    """Where one pane lands in the client's grid (0-based, cells)."""
    session_id: str
    x: int
    y: int
    width: int
    height: int


def _is_leaf(node) -> bool:
    return not hasattr(node, "children")


def _weight(node, vertical: bool) -> int:
    """A node's size along the axis its parent divides.

    Uses real iTerm2 cell sizes so the composited layout keeps the proportions
    the user set up. A splitter's extent is the max of its children across the
    divider, and their sum along it.
    """
    if _is_leaf(node):
        g = node.grid_size
        return int(g.width) if vertical else int(g.height)

    same_axis = (node.vertical == vertical)
    parts = [_weight(c, vertical) for c in node.children]
    if not parts:
        return 1
    # Children laid out ALONG this axis add up; across it they overlap.
    return sum(parts) if same_axis else max(parts)


def _split(total: int, weights: List[int], gaps: int) -> List[int]:
    """Divide `total` cells among weights, reserving `gaps` cells for dividers.

    Every pane gets at least 1 row/col, and rounding leftovers go to the largest
    pane so the regions always fill the space exactly.
    """
    avail = total - gaps
    n = len(weights)
    if avail < n:
        # Not enough room for everyone; give what we can.
        return [1] * n

    tw = sum(weights) or n
    sizes = [max(1, (avail * w) // tw) for w in weights]

    # Fix up rounding drift.
    drift = avail - sum(sizes)
    while drift > 0:
        sizes[sizes.index(max(sizes))] += 1
        drift -= 1
    while drift < 0:
        i = sizes.index(max(sizes))
        if sizes[i] <= 1:
            break
        sizes[i] -= 1
        drift += 1
    return sizes


def regions(root, cols: int, rows: int) -> List[Region]:
    """Lay out every pane in a tab's split tree onto a cols x rows grid."""
    out: List[Region] = []

    def place(node, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return

        if _is_leaf(node):
            out.append(Region(node.session_id, x, y, w, h))
            return

        kids = list(node.children)
        if not kids:
            return
        if len(kids) == 1:
            place(kids[0], x, y, w, h)
            return

        vertical = bool(node.vertical)
        gaps = len(kids) - 1        # one divider line between each pair
        weights = [_weight(k, vertical) for k in kids]

        if vertical:
            # Children sit side by side; divide the WIDTH.
            widths = _split(w, weights, gaps)
            cx = x
            for i, (kid, kw) in enumerate(zip(kids, widths)):
                place(kid, cx, y, kw, h)
                cx += kw + (1 if i < len(kids) - 1 else 0)
        else:
            heights = _split(h, weights, gaps)
            cy = y
            for i, (kid, kh) in enumerate(zip(kids, heights)):
                place(kid, x, cy, w, kh)
                cy += kh + (1 if i < len(kids) - 1 else 0)

    place(root, 0, 0, cols, rows)
    return out
