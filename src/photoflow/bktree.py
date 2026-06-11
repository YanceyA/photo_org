"""BK-tree over perceptual hashes for fast near-neighbor lookup."""

from __future__ import annotations


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class BKTree:
    """BK-tree over 64-bit perceptual hashes for fast near-neighbor lookup."""

    def __init__(self):
        self.root = None
        self.children: dict[int, dict[int, int]] = {}

    def add(self, h: int):
        if self.root is None:
            self.root = h
            self.children[h] = {}
            return
        node = self.root
        while True:
            d = hamming(h, node)
            if d == 0:
                return
            nxt = self.children[node].get(d)
            if nxt is None:
                self.children[node][d] = h
                self.children.setdefault(h, {})
                return
            node = nxt

    def query(self, h: int, radius: int) -> list[int]:
        if self.root is None:
            return []
        hits, stack = [], [self.root]
        while stack:
            node = stack.pop()
            d = hamming(h, node)
            if d <= radius:
                hits.append(node)
            lo, hi = d - radius, d + radius
            for dist, child in self.children[node].items():
                if lo <= dist <= hi:
                    stack.append(child)
        return hits
