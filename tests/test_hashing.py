from pathlib import Path

from photoflow.bktree import BKTree
from photoflow.hashing import content_hash, hamming


class TestHamming:
    def test_identical(self):
        assert hamming(0xABCD, 0xABCD) == 0

    def test_one_bit(self):
        assert hamming(0b1000, 0b0000) == 1

    def test_many_bits(self):
        assert hamming(0xFFFF, 0x0000) == 16


class TestContentHash:
    def test_stable_and_distinct(self, tmp_path: Path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"hello world" * 1000)
        b.write_bytes(b"hello world" * 1000 + b"!")
        ha = content_hash(a)
        assert ha == content_hash(a)  # deterministic
        assert ha != content_hash(b)  # content-sensitive
        assert len(ha) == 40  # blake2b digest_size=20 -> 40 hex chars


class TestBKTree:
    def test_empty_query(self):
        assert BKTree().query(0, 5) == []

    def test_exact_member_found(self):
        t = BKTree()
        t.add(0b1010)
        assert t.query(0b1010, 0) == [0b1010]

    def test_radius_inclusion_and_exclusion(self):
        t = BKTree()
        for h in (0b0000, 0b0001, 0b0111, 0b1111111):
            t.add(h)
        hits = set(t.query(0b0000, 2))
        assert 0b0000 in hits and 0b0001 in hits
        assert 0b0111 not in hits  # distance 3 > radius 2
        assert 0b1111111 not in hits

    def test_duplicate_add_is_noop(self):
        t = BKTree()
        t.add(42)
        t.add(42)
        assert t.query(42, 0) == [42]
