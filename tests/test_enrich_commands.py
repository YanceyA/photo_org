"""Integration tests for the enrich command modules.

Models are faked (insightface/torch aren't installed); clustering uses real scikit-learn;
the XMP writeback is captured via a monkeypatched exiftool runner, with one real-exiftool
round-trip marked @pytest.mark.exiftool.
"""

import csv
import os
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoflow.config import Config
from photoflow.db import new_run, open_db
from photoflow.enrich import apply as eapply
from photoflow.enrich import assign as eassign
from photoflow.enrich import cluster as ecluster
from photoflow.enrich import deps as edeps
from photoflow.enrich import faces as efaces
from photoflow.enrich import merge as emerge
from photoflow.enrich import review as ereview
from photoflow.enrich import scan as escan
from photoflow.enrich import status as estatus
from photoflow.enrich import tagger as etagger
from photoflow.exiftool import ExiftoolResult, KeywordSets


def _seed(tmp_path: Path, n=3):
    workdir = tmp_path / "wd"
    conn = open_db(workdir)
    lib = tmp_path / "lib"
    lib.mkdir()
    ids = []
    for i in range(n):
        p = lib / f"img{i}.jpg"
        Image.new("RGB", (200, 150), (30 + i * 20, 60, 90)).save(p, "JPEG")
        cur = conn.execute(
            "INSERT INTO files(source_path, rel_path, ext, kind, status, dest_path, width, height,"
            " content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(p), f"img{i}.jpg", ".jpg", "image", "copied", str(p), 200, 150, f"h{i}"),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return conn, workdir, lib, ids


def _run(fn, conn, workdir, cfg=None, **argkw):
    run_id = new_run(conn, "enrich", {})
    args = types.SimpleNamespace(**argkw)
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id}.jsonl", "a", encoding="utf-8") as log_fh:
        fn(conn, workdir, run_id, log_fh, args, cfg or Config())


class FakeDetector:
    """One face per image; the embedding direction is keyed by `which` so we can build
    real clusters across files."""

    def __init__(self, which):
        self.which = which

    def detect(self, rgb):
        v = np.zeros(512, dtype=np.float32)
        v[self.which] = 1.0
        return [{"embedding": v, "bbox": (20.0, 20.0, 80.0, 100.0), "det_score": 0.9}]


class FakeTagger:
    source = "clip"

    def tag(self, im):
        # Scores derived from the configured bands so this stays correct across tagger-model
        # swaps (different models live on different score scales): one clearly-auto, one inside
        # the [review, accept) edge band, one clearly-dropped.
        a, r = Config().tag_score_accept, Config().tag_score_review
        return [("beach", a + 0.1), ("boat", (a + r) / 2), ("noise", r / 2)]  # auto/review/dropped


# --------------------------------------------------------------------------- scan


def test_scan_stores_faces_tags_and_is_incremental(tmp_path, monkeypatch):
    conn, workdir, lib, ids = _seed(tmp_path)
    monkeypatch.setattr(edeps, "HAVE_FACES", True)
    monkeypatch.setattr(efaces, "FaceDetector", lambda cfg: FakeDetector(which=0))
    monkeypatch.setattr(etagger, "build_tagger", lambda cfg, wd: FakeTagger())

    _run(escan.cmd_enrich_scan, conn, workdir)

    assert conn.execute("SELECT COUNT(*) c FROM faces").fetchone()["c"] == len(ids)
    # 'beach' auto, 'boat' review, 'noise' dropped (below tag_score_review)
    statuses = {r["tag"]: r["status"] for r in conn.execute("SELECT tag, status FROM tags")}
    assert statuses["beach"] == "auto" and statuses["boat"] == "review" and "noise" not in statuses
    assert (workdir / "faces").is_dir()
    assert all((workdir / r["thumb"]).exists() for r in conn.execute("SELECT thumb FROM faces"))

    # re-run: incremental skip, no duplicate faces
    _run(escan.cmd_enrich_scan, conn, workdir)
    assert conn.execute("SELECT COUNT(*) c FROM faces").fetchone()["c"] == len(ids)


class FlakyDetector(FakeDetector):
    """Raises on its first call only - a truncated JPEG / transient CUDA OOM."""

    def __init__(self, which=0):
        super().__init__(which)
        self.calls = 0

    def detect(self, rgb):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom: model blew up on this file")
        return super().detect(rgb)


def test_scan_survives_a_detector_failure_and_retries_next_run(tmp_path, monkeypatch):
    # E1: one bad file aborted the whole run and rolled back the batch.
    conn, workdir, lib, ids = _seed(tmp_path, n=3)
    monkeypatch.setattr(edeps, "HAVE_FACES", True)
    monkeypatch.setattr(efaces, "FaceDetector", lambda cfg: FlakyDetector(which=0))
    monkeypatch.setattr(etagger, "build_tagger", lambda cfg, wd: FakeTagger())

    _run(escan.cmd_enrich_scan, conn, workdir)  # must not raise

    assert conn.execute("SELECT COUNT(*) c FROM faces").fetchone()["c"] == 2  # 2 of 3 ok
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM actions WHERE action='enrich_detect_error'"
        ).fetchone()["c"]
        == 1
    )
    bad = conn.execute("SELECT * FROM enrich_state WHERE errors=1").fetchall()
    assert len(bad) == 1
    assert bad[0]["faces_done"] == 0  # not marked done -> retried next run
    assert bad[0]["tags_done"] == 1  # the tagger side still succeeded


def test_scan_gives_up_on_a_file_after_three_failures(tmp_path, monkeypatch, capsys):
    conn, workdir, lib, ids = _seed(tmp_path, n=2)
    conn.execute(
        "INSERT INTO enrich_state(file_id, faces_done, tags_done, errors, ts) VALUES (?,0,0,3,'')",
        (ids[0],),
    )
    conn.commit()
    detector = FakeDetector(which=0)
    calls = {"n": 0}
    original = detector.detect

    def counting(rgb):
        calls["n"] += 1
        return original(rgb)

    detector.detect = counting
    monkeypatch.setattr(edeps, "HAVE_FACES", True)
    monkeypatch.setattr(efaces, "FaceDetector", lambda cfg: detector)
    monkeypatch.setattr(etagger, "build_tagger", lambda cfg, wd: FakeTagger())

    _run(escan.cmd_enrich_scan, conn, workdir)

    assert calls["n"] == 1  # only the healthy file was processed
    out = capsys.readouterr().out
    assert "1 files to process" in out
    assert "repeated errors" in out


# --------------------------------------------------------------------------- cluster


FACE_COLS = [
    "cluster_id",
    "face_id",
    "file_id",
    "source_path",
    "cluster_prob",
    "suggested_person",
    "person",
    "decision",
]


def _face_row(cluster_id, face_id, file_id, person="", decision=""):
    return {
        "cluster_id": cluster_id,
        "face_id": face_id,
        "file_id": file_id,
        "source_path": "x",
        "cluster_prob": 0.9,
        "suggested_person": "",
        "person": person,
        "decision": decision,
    }


def _insert_face(conn, file_id, which, person_id=None):
    v = np.zeros(512, dtype=np.float32)
    v[which] = 1.0
    conn.execute(
        "INSERT INTO faces(file_id, bbox, det_score, embedding, img_w, img_h, person_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (file_id, "[0,0,10,10]", 0.9, v.tobytes(), 200, 150, person_id),
    )


def test_cluster_groups_unassigned_and_preserves_named(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    # two well-separated identities, >= min_cluster_size each
    for _ in range(6):
        _insert_face(conn, fid, which=0)
    for _ in range(6):
        _insert_face(conn, fid, which=300)
    conn.commit()

    _run(ecluster.cmd_enrich_cluster, conn, workdir)
    labels = [r["cluster_id"] for r in conn.execute("SELECT cluster_id FROM faces")]
    assert len({lbl for lbl in labels if lbl is not None}) == 2
    assert all(
        r["cluster_prob"] is not None for r in conn.execute("SELECT cluster_prob FROM faces")
    )

    # assign one identity to a person, re-cluster: assigned faces keep person_id, excluded
    conn.execute("INSERT INTO persons(name, created) VALUES ('Mum', '')")
    pid = conn.execute("SELECT id FROM persons").fetchone()["id"]
    conn.execute(
        "UPDATE faces SET person_id=? WHERE cluster_id=(SELECT MIN(cluster_id) FROM faces)", (pid,)
    )
    conn.commit()
    _run(ecluster.cmd_enrich_cluster, conn, workdir)
    assigned = conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id=?", (pid,)).fetchone()[
        "c"
    ]
    assert assigned == 6  # named faces untouched by re-cluster


# --------------------------------------------------------------------------- review


def test_cluster_passes_selection_knobs_from_config(tmp_path, monkeypatch):
    # Layer 1/2 knobs: cluster_selection_epsilon (merge burst fragments) + selection_method
    # (eom vs leaf, to split mega-clusters). cluster_embeddings accepts both; this guards that
    # the command threads the configured values through (defaults keep today's behavior).
    from dataclasses import replace

    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(6):
        _insert_face(conn, fid, which=0)
    for _ in range(6):
        _insert_face(conn, fid, which=300)
    conn.commit()

    captured: dict = {}
    real = ecluster.cluster_embeddings

    def spy(embs, **kw):
        captured.update(kw)
        return real(embs, **kw)

    monkeypatch.setattr(ecluster, "cluster_embeddings", spy)
    cfg = replace(
        Config(), enrich_cluster_selection_epsilon=0.35, enrich_cluster_selection_method="leaf"
    )
    run_id = new_run(conn, "enrich", {})
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        ecluster.cmd_enrich_cluster(conn, workdir, run_id, fh, types.SimpleNamespace(), cfg)
    assert captured["cluster_selection_epsilon"] == 0.35
    assert captured["cluster_selection_method"] == "leaf"


def test_cluster_skips_malformed_embeddings(tmp_path):
    # A damaged DB could hold a BLOB that isn't a clean float32[512]; cluster must skip it
    # with a clear message, not crash np.frombuffer / np.stack with an opaque error.
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(6):
        _insert_face(conn, fid, which=0)
    for _ in range(6):
        _insert_face(conn, fid, which=300)
    conn.execute(
        "INSERT INTO faces(file_id, bbox, det_score, embedding, img_w, img_h) VALUES (?,?,?,?,?,?)",
        (fid, "[0,0,1,1]", 0.9, b"\x01\x02\x03", 200, 150),  # 3 bytes: not a float32 vector
    )
    conn.commit()

    _run(ecluster.cmd_enrich_cluster, conn, workdir)  # must not raise

    good = [
        r["cluster_id"]
        for r in conn.execute("SELECT cluster_id FROM faces WHERE LENGTH(embedding)=2048")
    ]
    assert len({lbl for lbl in good if lbl is not None}) == 2  # valid faces still cluster
    bad = conn.execute("SELECT cluster_id FROM faces WHERE LENGTH(embedding)=3").fetchone()
    assert bad["cluster_id"] is None  # malformed face skipped, left unclustered


def test_review_emits_html_and_csvs_with_carry_forward(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(5):
        _insert_face(conn, fid, which=0)
    conn.execute("UPDATE faces SET cluster_id=1, cluster_prob=0.9")
    conn.execute(
        "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
        (fid, "boat", "clip", 0.4, "review"),
    )
    conn.commit()

    _run(ereview.cmd_enrich_review, conn, workdir)
    assert (workdir / "enrich_review.html").exists()
    assert (workdir / "faces.csv").exists() and (workdir / "tags.csv").exists()

    # carry-forward: record a decision, regenerate, decision survives
    faces_csv = workdir / "faces.csv"
    rows = list(csv.DictReader(faces_csv.open(encoding="utf-8")))
    rows[0]["person"], rows[0]["decision"] = "Mum", "keep"
    with faces_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    _run(ereview.cmd_enrich_review, conn, workdir)
    again = list(csv.DictReader(faces_csv.open(encoding="utf-8")))
    kept = [r for r in again if r["decision"] == "keep"]
    assert kept and kept[0]["person"] == "Mum"


# --------------------------------------------------------------------------- apply


def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def _fake_exiftool(captured):
    """Stand in for exiftool_apply_argfile: collect every batch's lines, report success."""

    def run(lines):
        captured.setdefault("lines", []).extend(lines)
        return ExiftoolResult(0, "", "")

    return run


def test_apply_builds_region_and_keyword_args(tmp_path, monkeypatch):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    _insert_face(conn, fid, which=0)
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    conn.execute(
        "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
        (fid, "beach", "clip", 0.9, "auto"),
    )
    conn.execute(
        "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
        (fid, "boat", "clip", 0.4, "review"),
    )
    conn.commit()

    _write_csv(
        workdir / "faces.csv",
        [
            "cluster_id",
            "face_id",
            "file_id",
            "source_path",
            "cluster_prob",
            "suggested_person",
            "person",
            "decision",
        ],
        [
            {
                "cluster_id": 1,
                "face_id": face_id,
                "file_id": fid,
                "source_path": "x",
                "cluster_prob": 0.9,
                "suggested_person": "",
                "person": "Mum",
                "decision": "keep",
            }
        ],
    )
    _write_csv(
        workdir / "tags.csv",
        ["file_id", "tag", "source", "score", "suggestion", "decision"],
        [
            {
                "file_id": fid,
                "tag": "boat",
                "source": "clip",
                "score": 0.4,
                "suggestion": "review",
                "decision": "keep",
            }
        ],
    )

    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False)

    lines = captured["lines"]
    assert "-XMP-mwg-rs:RegionName=Mum" in lines
    assert "-XMP-dc:Subject=Mum" in lines
    assert "-XMP-dc:Subject=beach" in lines  # auto tag applied
    assert "-XMP-dc:Subject=boat" in lines  # review tag kept
    # person became durable
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()[
        "person_id"
    ]
    assert (
        conn.execute("SELECT applied FROM enrich_state WHERE file_id=?", (fid,)).fetchone()[
            "applied"
        ]
        == 1
    )


def test_apply_writes_sidecar_target_for_raw(tmp_path, monkeypatch):
    # RAW/video get a .xmp SIDECAR, never an embed into the container (invariant #6).
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    conn.execute("UPDATE files SET ext='.dng', dest_path=? WHERE id=?", ("C:/lib/img0.dng", fid))
    _insert_face(conn, fid, which=0)
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    conn.commit()
    _write_csv(
        workdir / "faces.csv",
        [
            "cluster_id",
            "face_id",
            "file_id",
            "source_path",
            "cluster_prob",
            "suggested_person",
            "person",
            "decision",
        ],
        [
            {
                "cluster_id": 1,
                "face_id": face_id,
                "file_id": fid,
                "source_path": "x",
                "cluster_prob": 0.9,
                "suggested_person": "",
                "person": "Mum",
                "decision": "keep",
            }
        ],
    )
    _write_csv(
        workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
    )

    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False)

    lines = captured["lines"]
    # the write TARGET is the sidecar, and the raw container is never a write target
    assert "C:/lib/img0.dng.xmp" in lines
    assert "C:/lib/img0.dng" not in lines


def test_apply_respects_blacklist_and_rejects(tmp_path, monkeypatch):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    conn.execute(
        "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
        (fid, "person", "ram", None, "auto"),
    )  # ubiquitous junk tag
    conn.commit()
    _write_csv(
        workdir / "faces.csv",
        [
            "cluster_id",
            "face_id",
            "file_id",
            "source_path",
            "cluster_prob",
            "suggested_person",
            "person",
            "decision",
        ],
        [],
    )
    _write_csv(
        workdir / "tags.csv",
        ["file_id", "tag", "source", "score", "suggestion", "decision"],
        [
            {
                "file_id": "*",
                "tag": "person",
                "source": "",
                "score": "",
                "suggestion": "auto",
                "decision": "reject",
            }
        ],
    )
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False)
    # blacklisted tag never written; nothing to write for this file at all
    assert "-XMP-dc:Subject=person" not in captured.get("lines", [])


@pytest.mark.exiftool
def test_apply_real_exiftool_roundtrip(tmp_path):
    import json
    import subprocess

    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    _insert_face(conn, fid, which=0)
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    conn.execute("UPDATE faces SET bbox=? WHERE id=?", ("[20,20,80,100]", face_id))
    conn.execute(
        "INSERT INTO tags(file_id, tag, source, score, status) VALUES (?,?,?,?,?)",
        (fid, "beach", "clip", 0.9, "auto"),
    )
    conn.commit()
    _write_csv(
        workdir / "faces.csv",
        [
            "cluster_id",
            "face_id",
            "file_id",
            "source_path",
            "cluster_prob",
            "suggested_person",
            "person",
            "decision",
        ],
        [
            {
                "cluster_id": 1,
                "face_id": face_id,
                "file_id": fid,
                "source_path": "x",
                "cluster_prob": 0.9,
                "suggested_person": "",
                "person": "Mum",
                "decision": "keep",
            }
        ],
    )
    _write_csv(
        workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
    )

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False)

    dest = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()["dest_path"]
    out = subprocess.run(
        ["exiftool", "-j", "-XMP-dc:Subject", "-XMP-mwg-rs:RegionName", dest],
        capture_output=True,
        text=True,
    )
    rec = json.loads(out.stdout)[0]
    subjects = rec.get("Subject")
    subjects = [subjects] if isinstance(subjects, str) else (subjects or [])
    assert "beach" in subjects and "Mum" in subjects
    names = rec.get("RegionName")
    names = [names] if isinstance(names, str) else (names or [])
    assert "Mum" in names


def _one_face_file(tmp_path, person="Mum", n=1):
    """Seed n library files, one face each, plus a faces.csv naming them all `person`."""
    conn, workdir, lib, ids = _seed(tmp_path, n=n)
    rows = []
    for fid in ids:
        _insert_face(conn, fid, which=0)
        face_id = conn.execute("SELECT MAX(id) m FROM faces").fetchone()["m"]
        conn.execute("UPDATE faces SET bbox=? WHERE id=?", ("[20,20,80,100]", face_id))
        rows.append(_face_row(1, face_id, fid, person=person, decision="keep"))
    conn.commit()
    _write_csv(workdir / "faces.csv", FACE_COLS, rows)
    _write_csv(
        workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
    )
    return conn, workdir, lib, ids


def test_apply_is_incremental_on_the_second_run(tmp_path, monkeypatch, capsys):
    # H10: apply rewrote every enriched file on every run (9 runs = 9 full-library rewrites,
    # 9 mtime bumps). A per-file signature of what we'd write makes the second run a no-op.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
    assert captured["lines"], "first run must write"
    sig1 = conn.execute("SELECT applied_sig FROM enrich_state").fetchone()["applied_sig"]
    assert sig1

    captured.clear()
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
    out = capsys.readouterr().out
    assert captured.get("lines", []) == []  # nothing rewritten
    assert "written 0" in out and "unchanged 1" in out
    assert conn.execute("SELECT applied_sig FROM enrich_state").fetchone()["applied_sig"] == sig1

    # --all forces the rewrite back on
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=True)
    assert captured["lines"]


def test_apply_rewrites_only_the_file_whose_people_changed(tmp_path, monkeypatch):
    conn, workdir, lib, ids = _one_face_file(tmp_path, n=2)
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    # rename the person on ONE file only. A rename is a DB-side operation (`enrich merge`);
    # step 1 only ever grants a FIRST name from faces.csv, so the stale keep row naming the
    # face "Mum" no longer overwrites this (R8).
    target = conn.execute("SELECT id FROM faces WHERE file_id=?", (ids[0],)).fetchone()["id"]
    conn.execute("INSERT INTO persons(name, created) VALUES ('Mother','')")
    mother = conn.execute("SELECT id FROM persons WHERE name='Mother'").fetchone()["id"]
    conn.execute("UPDATE faces SET person_id=? WHERE id=?", (mother, target))
    conn.commit()

    untouched_sig = conn.execute(
        "SELECT applied_sig FROM enrich_state WHERE file_id=?", (ids[1],)
    ).fetchone()["applied_sig"]

    captured.clear()
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
    dests = {r["dest_path"] for r in conn.execute("SELECT id, dest_path FROM files")}
    assert len(dests) == 2  # the two library files really are distinct write targets
    written = [ln for ln in captured["lines"] if ln in dests]
    changed = conn.execute("SELECT dest_path FROM files WHERE id=?", (ids[0],)).fetchone()
    assert written == [changed["dest_path"]]  # only the changed file was rewritten
    assert (
        conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (ids[1],)).fetchone()[
            "applied_sig"
        ]
        == untouched_sig
    )


def test_apply_dry_run_mutates_nothing(tmp_path, monkeypatch):
    # R2: the dry run used to durably commit the step-1 person upsert + faces.person_id,
    # hiding those clusters from the next `enrich review`.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool({}))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=True, all=False)

    assert conn.execute("SELECT COUNT(*) c FROM persons").fetchone()["c"] == 0
    assert (
        conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NOT NULL").fetchone()["c"]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) c FROM enrich_state").fetchone()["c"] == 0


def test_apply_skips_files_whose_keyword_read_failed(tmp_path, monkeypatch, capsys):
    # R1: one corrupt XMP makes read_keywords return {} for its whole 200-file batch. Falling
    # back to existing=set() and clearing dc:Subject would wipe every pre-existing keyword.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    out = capsys.readouterr().out
    assert captured.get("lines", []) == []  # nothing written rather than everything clobbered
    assert "skipped-unreadable 1" in out
    assert conn.execute("SELECT COUNT(*) c FROM enrich_state").fetchone()["c"] == 0


def test_apply_failed_batch_keeps_the_old_signature(tmp_path, monkeypatch, capsys):
    # E2: a read-only/locked file made exiftool exit non-zero and apply reported success.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(
        eapply,
        "exiftool_apply_argfile",
        lambda lines: ExiftoolResult(
            returncode=1, stderr="Error: img0.jpg is not writable", stdout=""
        ),
    )

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    out = capsys.readouterr().out
    assert "failed 1" in out and "not writable" in out
    row = conn.execute("SELECT applied_sig FROM enrich_state").fetchone()
    assert row is None or row["applied_sig"] is None  # never marked applied


def test_apply_rewrites_when_write_config_changes(tmp_path, monkeypatch):
    # The signature must cover the cfg switches that gate real output. Without them, editing
    # photoflow.toml writes nothing and `--all` is the only (undiscoverable) recovery.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
    assert any(ln.startswith("-XMP-mwg-rs:") for ln in captured["lines"])

    captured.clear()
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)
    assert captured.get("lines", []) == []  # same config -> still a no-op

    flipped = replace(Config(), write_mwg_regions=False)
    _run(eapply.cmd_enrich_apply, conn, workdir, cfg=flipped, dry_run=False, all=False)
    assert captured["lines"]  # the config change invalidated the signature
    assert not any(ln.startswith("-XMP-mwg-rs:") for ln in captured["lines"])


def test_apply_retries_a_failed_batch_per_file(tmp_path, monkeypatch, capsys):
    # exiftool keeps going past a bad file and writes the good -execute blocks, so a non-zero
    # batch rc must not strand the batch's healthy files without an applied_sig forever (they
    # would be re-read and rewritten on every future run - H10 in miniature).
    conn, workdir, lib, ids = _one_face_file(tmp_path, n=2)
    bad = conn.execute("SELECT dest_path FROM files WHERE id=?", (ids[1],)).fetchone()["dest_path"]
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})

    def flaky(lines):
        if bad in lines:
            return ExiftoolResult(returncode=1, stderr=f"Error: {bad} is not writable", stdout="")
        return ExiftoolResult(0, "", "")

    monkeypatch.setattr(eapply, "exiftool_apply_argfile", flaky)

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    out = capsys.readouterr().out
    assert "written 1" in out and "failed 1" in out
    good = conn.execute(
        "SELECT applied_sig FROM enrich_state WHERE file_id=?", (ids[0],)
    ).fetchone()
    assert good and good["applied_sig"]  # the healthy file kept its write
    assert (
        conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (ids[1],)).fetchone()
        is None
    )


def test_apply_creates_a_missing_sidecar_target(tmp_path, monkeypatch, capsys):
    # A .xmp sidecar that doesn't exist yet has no keywords to clobber, so write it. Counting
    # it "unreadable" would strand it forever: no rerun and not even --all could create it.
    conn, workdir, lib, ids = _one_face_file(tmp_path)
    raw = str(lib / "img0.dng")
    conn.execute("UPDATE files SET ext='.dng', dest_path=? WHERE id=?", (raw, ids[0]))
    conn.commit()
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {})  # no record for a missing file
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    out = capsys.readouterr().out
    assert raw + ".xmp" in captured["lines"]
    assert "skipped-unreadable 0" in out and "written 1" in out


@pytest.mark.exiftool
def test_apply_preserves_library_mtime(tmp_path):
    # H9: -overwrite_original without -P resets mtime to "now" on every apply, and HANDOFF
    # §2.1 promises the library mtime is the source mtime.
    import os

    conn, workdir, lib, ids = _one_face_file(tmp_path)
    dest = conn.execute("SELECT dest_path FROM files").fetchone()["dest_path"]
    os.utime(dest, (1_000_000_000, 1_000_000_000))
    before = os.stat(dest).st_mtime

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    assert os.stat(dest).st_mtime == pytest.approx(before, abs=2)


def test_apply_preserves_foreign_entries_without_exiftool(tmp_path, monkeypatch):
    # Same guarantee as the round-trip below, but through a fake read_keywords so CI (no
    # exiftool) still covers the KeywordSets/owned_people wiring in cmd_enrich_apply.
    conn, workdir, lib, ids = _one_face_file(tmp_path, person="Mum")
    # "Old" is a name photoflow OWNS but no longer assigns on this file; "Grandma" is foreign.
    conn.execute("INSERT INTO persons(name, created) VALUES ('Old','x')")
    conn.commit()
    existing = KeywordSets(
        subject={"Holiday"},
        hierarchical={"Places|Paris", "People|Old"},
        persons={"Grandma", "Old"},
    )
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: existing for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    lines = captured["lines"]
    assert "-XMP-lr:HierarchicalSubject=Places|Paris" in lines  # foreign hierarchy kept
    assert "-XMP-iptcExt:PersonInImage=Grandma" in lines  # foreign person kept
    assert "-XMP-dc:Subject=Holiday" in lines  # existing keyword kept
    assert "-XMP-lr:HierarchicalSubject=People|Mum" in lines  # ours written
    assert "-XMP-iptcExt:PersonInImage=Mum" in lines
    assert "-XMP-lr:HierarchicalSubject=People|Old" not in lines  # ours, unassigned -> gone
    assert "-XMP-iptcExt:PersonInImage=Old" not in lines


@pytest.mark.exiftool
def test_apply_preserves_foreign_hierarchy_and_person(tmp_path):
    # H11 end-to-end: values another tool wrote survive an apply.
    import json
    import subprocess

    conn, workdir, lib, ids = _one_face_file(tmp_path, person="Yancey")
    dest = conn.execute("SELECT dest_path FROM files").fetchone()["dest_path"]
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-XMP-lr:HierarchicalSubject=Places|Paris",
            "-XMP-iptcExt:PersonInImage=Grandma",
            dest,
        ],
        capture_output=True,
        check=True,
    )

    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    out = subprocess.run(
        ["exiftool", "-j", "-XMP-lr:HierarchicalSubject", "-XMP-iptcExt:PersonInImage", dest],
        capture_output=True,
        text=True,
        check=True,
    )
    rec = json.loads(out.stdout)[0]

    def as_list(v):
        return [v] if isinstance(v, str) else (v or [])

    hier = as_list(rec.get("HierarchicalSubject"))
    persons = as_list(rec.get("PersonInImage"))
    assert "Places|Paris" in hier and "People|Yancey" in hier
    assert "Grandma" in persons and "Yancey" in persons


# ----------------------------------------------------- assign (centroid label propagation)


def _make_person(conn, name, file_id, which, n=5):
    conn.execute("INSERT INTO persons(name, created) VALUES (?, '')", (name,))
    pid = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()["id"]
    for _ in range(n):
        _insert_face(conn, file_id, which=which, person_id=pid)
    return pid


def test_assign_propagates_named_centroid_to_near_faces(tmp_path):
    # An unassigned face near a named person's centroid is auto-assigned; a far face is left
    # for clustering. This mops up burst-fragments + noise of people you've already named.
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    pid = _make_person(conn, "Mum", fid, which=0)
    _insert_face(conn, fid, which=0)  # near Mum's centroid -> assign
    _insert_face(conn, fid, which=300)  # far -> leave unassigned
    conn.commit()
    unassigned = [r["id"] for r in conn.execute("SELECT id FROM faces WHERE person_id IS NULL")]

    _run(eassign.cmd_enrich_assign, conn, workdir, dry_run=False, min_sim=None)

    persons_now = {
        r["id"]: r["person_id"]
        for r in conn.execute("SELECT id, person_id FROM faces WHERE id IN (?,?)", unassigned)
    }
    assigned = [fidx for fidx, p in persons_now.items() if p == pid]
    left = [fidx for fidx, p in persons_now.items() if p is None]
    assert len(assigned) == 1 and len(left) == 1  # near -> Mum, far -> still unassigned


def test_assign_skips_ignored_faces(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    _make_person(conn, "Mum", fid, which=0)
    _insert_face(conn, fid, which=0)  # near Mum but marked "not interested"
    ignored_id = conn.execute("SELECT MAX(id) m FROM faces").fetchone()["m"]
    conn.execute("UPDATE faces SET ignored=1 WHERE id=?", (ignored_id,))
    conn.commit()

    _run(eassign.cmd_enrich_assign, conn, workdir, dry_run=False, min_sim=None)
    row = conn.execute("SELECT person_id FROM faces WHERE id=?", (ignored_id,)).fetchone()
    assert row["person_id"] is None


def test_assign_dry_run_changes_nothing(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    _make_person(conn, "Mum", fid, which=0)
    _insert_face(conn, fid, which=0)
    target = conn.execute("SELECT MAX(id) m FROM faces").fetchone()["m"]
    conn.commit()

    _run(eassign.cmd_enrich_assign, conn, workdir, dry_run=True, min_sim=None)
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=?", (target,)).fetchone()["person_id"]
        is None
    )


def test_assign_writes_review_html_named_by_threshold(tmp_path):
    # The dry-run must emit a static assign_review_sim<val>.html so the user can eyeball which
    # proposed faces are wrong at a given --min-sim (counts alone can't show that).
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    _make_person(conn, "Mum", fid, which=0)
    _insert_face(conn, fid, which=0)  # exact match -> sim 1.00
    conn.commit()

    _run(eassign.cmd_enrich_assign, conn, workdir, dry_run=True, min_sim=0.5)

    htmls = list(workdir.glob("assign_review_sim0.50.html"))
    assert htmls, "dry-run should write a threshold-named review page"
    text = htmls[0].read_text(encoding="utf-8")
    assert "Mum" in text and "1.00" in text  # proposed face grouped under Mum at sim 1.00


def test_assign_without_persons_is_graceful(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    _insert_face(conn, ids[0], which=0)
    conn.commit()
    _run(eassign.cmd_enrich_assign, conn, workdir, dry_run=False, min_sim=None)  # must not raise


# ----------------------------------------------------- merge duplicate person names


def test_merge_consolidates_duplicate_persons(tmp_path, monkeypatch):
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    cid = _make_person(conn, "Deirdre Hough", fid, which=0, n=2)
    _make_person(conn, "Deidre Hough", fid, which=1, n=3)  # misspelled
    _make_person(conn, "Deirdre hough", fid, which=2, n=1)  # wrong case
    conn.commit()

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough", "Deirdre hough"],
    )

    names = {r["name"] for r in conn.execute("SELECT name FROM persons")}
    assert names == {"Deirdre Hough"}  # aliases removed
    moved = conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id=?", (cid,)).fetchone()["c"]
    assert moved == 6  # every alias face repointed to the canonical person
    assert conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NULL").fetchone()["c"] == 0


def test_merge_creates_canonical_when_absent(tmp_path, monkeypatch):
    # only misspellings exist; the correct name is new -> it's created and absorbs them
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    _make_person(conn, "Yancey arrington", ids[0], which=0, n=2)
    conn.commit()
    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Yancey Arrington",
        aliases=["Yancey arrington"],
    )
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Yancey Arrington"}
    assert (
        conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NOT NULL").fetchone()["c"]
        == 2
    )


def test_merge_ignores_unknown_or_self_alias(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    _make_person(conn, "Mum", ids[0], which=0, n=2)
    conn.commit()
    _run(emerge.cmd_enrich_merge, conn, workdir, canonical="Mum", aliases=["Mum", "Nobody"])
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Mum"}  # unchanged
    assert "no such person: 'Nobody'" in capsys.readouterr().out
    assert captured.get("lines", []) == []  # nothing was written to any file


def _named_library_file(tmp_path, name):
    """One library file whose single face is assigned to `name`, already applied."""
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    conn.execute("INSERT INTO persons(name, created) VALUES (?, '')", (name,))
    pid = conn.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()["id"]
    _insert_face(conn, fid, which=0, person_id=pid)
    conn.execute(
        "INSERT INTO enrich_state(file_id, applied, applied_sig, ts) VALUES (?,1,'deadbeef','')",
        (fid,),
    )
    conn.commit()
    return conn, workdir, lib, fid


def test_merge_invalidates_applied_sig_for_touched_files(tmp_path, monkeypatch):
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    # the stale name is stripped from the file...
    assert "-XMP-dc:Subject-=Deidre Hough" in captured["lines"]
    assert "-XMP-iptcExt:PersonInImage-=Deidre Hough" in captured["lines"]
    assert "-P" in captured["lines"] and "-overwrite_original" in captured["lines"]
    # ...and the file is queued for a rewrite so regions/PersonInImage get the new name
    row = conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (fid,)).fetchone()
    assert row["applied_sig"] is None
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}


def test_merge_rewrites_faces_csv_in_place(tmp_path, monkeypatch):
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    _write_csv(
        workdir / "faces.csv",
        FACE_COLS,
        [_face_row(1, face_id, fid, person="Deidre Hough", decision="keep")],
    )
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    rows = list(csv.DictReader((workdir / "faces.csv").open(encoding="utf-8")))
    assert [r["person"] for r in rows] == ["Deirdre Hough"]


def test_merge_aborts_without_exiftool(tmp_path, monkeypatch, capsys):
    # The strip is what makes the rename removable at all: once the alias persons row is gone
    # the name reads as foreign and apply preserves it forever. No exiftool -> change nothing.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    monkeypatch.setattr(emerge, "exiftool_available", lambda: False)

    def _boom(lines):  # pragma: no cover - must never be reached
        raise AssertionError("exiftool must not run when it is unavailable")

    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _boom)

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    out = capsys.readouterr().out
    assert "exiftool not found on PATH" in out
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deidre Hough"}
    named = conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NOT NULL").fetchone()
    assert named["c"] == 1
    assert (
        conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (fid,)).fetchone()[
            "applied_sig"
        ]
        == "deadbeef"
    )


def test_merge_keeps_alias_row_when_strip_fails(tmp_path, monkeypatch, capsys):
    # If the old name could not be removed from the file, the persons row has to stay: it is
    # the only thing that makes the name "ours" and therefore removable by a later apply.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(
        emerge, "exiftool_apply_argfile", lambda lines: ExiftoolResult(1, "Error: locked", "")
    )

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    out = capsys.readouterr().out
    assert "kept persons row" in out
    names = {r["name"] for r in conn.execute("SELECT name FROM persons")}
    assert names == {"Deirdre Hough", "Deidre Hough"}  # the alias row survives
    # the face stays on the alias, so a re-run finds the file again and retries the strip
    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()["person_id"]
        == alias_id
    )
    # the file was not rewritten, so its signature must not be invalidated either
    assert (
        conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (fid,)).fetchone()[
            "applied_sig"
        ]
        == "deadbeef"
    )
    acts = [
        r["action"]
        for r in conn.execute("SELECT action FROM actions WHERE action='enrich_merge_strip_error'")
    ]
    assert acts == ["enrich_merge_strip_error"]


def test_merge_keeps_alias_row_when_the_embed_target_is_missing(tmp_path, monkeypatch, capsys):
    # A missing EMBEDDED target is not "nothing to strip" - the file may come back still
    # carrying the old name, so it counts as a failure (an absent sidecar does not).
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    dest = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()["dest_path"]
    Path(dest).unlink()
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    out = capsys.readouterr().out
    assert "file not found" in out and "kept persons row" in out
    assert captured.get("lines", []) == []  # nothing to write
    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()["person_id"]
        == alias_id
    )
    detail = conn.execute(
        "SELECT detail FROM actions WHERE action='enrich_merge_strip_error'"
    ).fetchone()["detail"]
    assert "file not found" in detail


def test_merge_rerun_retries_failed_files(tmp_path, monkeypatch):
    # The recovery path: the first merge could not strip the file, so nothing was folded;
    # the second one succeeds and completes the merge.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(
        emerge, "exiftool_apply_argfile", lambda lines: ExiftoolResult(1, "Error: locked", "")
    )
    kw = dict(canonical="Deirdre Hough", aliases=["Deidre Hough"])
    _run(emerge.cmd_enrich_merge, conn, workdir, **kw)

    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()["person_id"]
        == alias_id
    )

    captured = {}
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))
    _run(emerge.cmd_enrich_merge, conn, workdir, **kw)

    assert "-XMP-dc:Subject-=Deidre Hough" in captured["lines"]  # the strip was retried
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}
    canonical = conn.execute("SELECT id FROM persons").fetchone()["id"]
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()["person_id"]
        == canonical
    )
    assert (
        conn.execute("SELECT applied_sig FROM enrich_state WHERE file_id=?", (fid,)).fetchone()[
            "applied_sig"
        ]
        is None
    )


def test_merge_drops_a_faceless_alias_row(tmp_path, monkeypatch):
    # Re-running a merge whose strip failed: the faces are already repointed, so there is
    # nothing to strip and the leftover alias row is simply dropped.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deirdre Hough")
    conn.execute("INSERT INTO persons(name, created) VALUES ('Deidre Hough','')")
    conn.commit()
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}
    assert captured.get("lines", []) == []  # no file had the alias -> no exiftool write


def test_apply_with_a_stale_csv_does_not_resurrect_a_merged_alias(tmp_path, monkeypatch):
    # R8: merge deletes the alias person row, then apply replayed the old faces.csv keep row
    # and re-created it, repointing the faces back.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    face_id = conn.execute("SELECT id FROM faces").fetchone()["id"]
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool({}))
    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    # simulate a stale CSV coming back (restored from the page's localStorage, or a backup)
    _write_csv(
        workdir / "faces.csv",
        FACE_COLS,
        [_face_row(1, face_id, fid, person="Deidre Hough", decision="keep")],
    )
    _write_csv(
        workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
    )
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool({}))
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False, all=False)

    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}
    pid = conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()
    canonical = conn.execute("SELECT id FROM persons").fetchone()["id"]
    assert pid["person_id"] == canonical


@pytest.mark.exiftool
def test_merge_removes_the_alias_from_the_real_file(tmp_path):
    import json
    import subprocess

    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    dest = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()["dest_path"]
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-XMP-dc:Subject=Deidre Hough",
            "-XMP-dc:Subject=beach",
            "-IPTC:Keywords=Deidre Hough",
            "-IPTC:Keywords=beach",
            "-XMP-lr:HierarchicalSubject=People|Deidre Hough",
            "-XMP-lr:HierarchicalSubject=Place|Beach",
            "-XMP-iptcExt:PersonInImage=Deidre Hough",
            dest,
        ],
        capture_output=True,
        check=True,
    )
    epoch = 1300000000  # H9: -P must keep the library mtime exactly where it was
    os.utime(dest, (epoch, epoch))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    rec = json.loads(
        subprocess.run(
            [
                "exiftool",
                "-j",
                "-XMP-dc:Subject",
                "-IPTC:Keywords",
                "-XMP-lr:HierarchicalSubject",
                "-XMP-iptcExt:PersonInImage",
                dest,
            ],
            capture_output=True,
            text=True,
        ).stdout
    )[0]

    def as_list(v):
        return [v] if isinstance(v, str) else (v or [])

    assert "Deidre Hough" not in as_list(rec.get("Subject"))
    assert "beach" in as_list(rec.get("Subject"))  # other keywords untouched
    assert "Deidre Hough" not in as_list(rec.get("Keywords"))
    assert "beach" in as_list(rec.get("Keywords"))
    assert "People|Deidre Hough" not in as_list(rec.get("HierarchicalSubject"))
    assert "Place|Beach" in as_list(rec.get("HierarchicalSubject"))
    assert "Deidre Hough" not in as_list(rec.get("PersonInImage"))
    assert os.path.getmtime(dest) == epoch


def test_merge_strip_failure_leaves_the_canonical_face_alone(tmp_path, monkeypatch):
    # One file carrying BOTH people: only the alias's own face may be held back, the
    # canonical person's face on the same file must not be dragged onto the alias.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    conn.execute("INSERT INTO persons(name, created) VALUES ('Deirdre Hough','')")
    canon_id = conn.execute("SELECT id FROM persons WHERE name='Deirdre Hough'").fetchone()["id"]
    _insert_face(conn, fid, which=1, person_id=canon_id)
    conn.commit()
    alias_face = conn.execute("SELECT id FROM faces WHERE person_id=?", (alias_id,)).fetchone()[
        "id"
    ]
    canon_face = conn.execute("SELECT id FROM faces WHERE person_id=?", (canon_id,)).fetchone()[
        "id"
    ]
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(
        emerge, "exiftool_apply_argfile", lambda lines: ExiftoolResult(1, "Error: locked", "")
    )

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    owner = {r["id"]: r["person_id"] for r in conn.execute("SELECT id, person_id FROM faces")}
    assert owner[alias_face] == alias_id  # held back for the retry
    assert owner[canon_face] == canon_id  # never belonged to the alias


def test_merge_strips_the_good_file_and_keeps_the_bad_one(tmp_path, monkeypatch, capsys):
    conn, workdir, lib, ids = _seed(tmp_path, n=2)
    conn.execute("INSERT INTO persons(name, created) VALUES ('Deidre Hough','')")
    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    for fid in ids:
        _insert_face(conn, fid, which=0, person_id=alias_id)
        conn.execute(
            "INSERT INTO enrich_state(file_id, applied, applied_sig, ts)"
            " VALUES (?,1,'deadbeef','')",
            (fid,),
        )
    conn.commit()
    dests = {r["id"]: r["dest_path"] for r in conn.execute("SELECT id, dest_path FROM files")}
    good, bad = ids[0], ids[1]
    captured = {}

    def flaky(lines):
        captured.setdefault("lines", []).extend(lines)
        # the batch holds both files -> rc=1; the per-file retry then separates them
        return ExiftoolResult(1 if dests[bad] in lines else 0, "Error: locked", "")

    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", flaky)

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    out = capsys.readouterr().out
    assert "stripped the old name(s) from 1 library file(s)" in out
    assert "on 1 file(s) whose strip failed" in out
    canon_id = conn.execute("SELECT id FROM persons WHERE name='Deirdre Hough'").fetchone()["id"]
    assert conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()  # kept
    owner = {
        r["file_id"]: r["person_id"] for r in conn.execute("SELECT file_id, person_id FROM faces")
    }
    assert owner[good] == canon_id and owner[bad] == alias_id
    sigs = {
        r["file_id"]: r["applied_sig"]
        for r in conn.execute("SELECT file_id, applied_sig FROM enrich_state")
    }
    assert sigs[good] is None and sigs[bad] == "deadbeef"


def test_merge_folds_an_alias_whose_sidecar_was_never_written(tmp_path, monkeypatch):
    # A RAW whose .xmp apply has never created holds no keywords: "nothing to strip", not a
    # failure - so the alias is folded and its row dropped.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    raw = lib / "img0.dng"
    conn.execute("UPDATE files SET ext='.dng', dest_path=? WHERE id=?", (str(raw), fid))
    conn.commit()
    assert not Path(str(raw) + ".xmp").exists()
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    assert captured.get("lines", []) == []
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}
    assert conn.execute("SELECT COUNT(*) c FROM faces WHERE person_id IS NULL").fetchone()["c"] == 0


def test_merge_strips_the_tags_a_previous_config_wrote(tmp_path, monkeypatch):
    # The file was applied under the default config; today's toml turned IPTC mirroring off
    # and renamed the hierarchy prefix. The strip must still clean up what is on the file.
    conn, workdir, lib, fid = _named_library_file(tmp_path, "Deidre Hough")
    captured = {}
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    monkeypatch.setattr(emerge, "exiftool_apply_argfile", _fake_exiftool(captured))
    cfg = replace(Config(), write_iptc_keywords=False, people_keyword_prefix="Faces")

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        cfg=cfg,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    lines = captured["lines"]
    assert "-IPTC:Keywords-=Deidre Hough" in lines
    assert "-XMP-lr:HierarchicalSubject-=Faces|Deidre Hough" in lines
    assert "-XMP-lr:HierarchicalSubject-=People|Deidre Hough" in lines


def test_merge_chunks_the_strip(tmp_path, monkeypatch):
    conn, workdir, lib, ids = _seed(tmp_path, n=3)
    conn.execute("INSERT INTO persons(name, created) VALUES ('Deidre Hough','')")
    alias_id = conn.execute("SELECT id FROM persons WHERE name='Deidre Hough'").fetchone()["id"]
    for fid in ids:
        _insert_face(conn, fid, which=0, person_id=alias_id)
    conn.commit()
    monkeypatch.setattr(emerge, "STRIP_BATCH", 2)
    monkeypatch.setattr(emerge, "exiftool_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        emerge,
        "exiftool_apply_argfile",
        lambda lines: calls.append(lines.count("-execute")) or ExiftoolResult(0, "", ""),
    )

    _run(
        emerge.cmd_enrich_merge,
        conn,
        workdir,
        canonical="Deirdre Hough",
        aliases=["Deidre Hough"],
    )

    assert calls == [2, 1]  # 3 files at STRIP_BATCH=2 -> two exiftool processes
    assert {r["name"] for r in conn.execute("SELECT name FROM persons")} == {"Deirdre Hough"}


# ----------------------------------------------------- "not interested" faces stay gone


def test_apply_marks_fully_dismissed_cluster_ignored(tmp_path, monkeypatch):
    # A cluster the user dismissed ("not interested") arrives as all-skip rows in faces.csv:
    # apply must mark those faces ignored (durable) so re-cluster/review never resurfaces them.
    # A single ejected face inside an otherwise-named cluster is NOT a dismiss -> stays eligible.
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(3):
        _insert_face(conn, fid, which=0)  # cluster 1: dismissed
    _insert_face(conn, fid, which=10)  # cluster 2: named
    _insert_face(conn, fid, which=11)  # cluster 2: ejected member
    conn.commit()
    face_ids = [r["id"] for r in conn.execute("SELECT id FROM faces ORDER BY id")]
    c1, c2 = face_ids[:3], face_ids[3:]

    _write_csv(
        workdir / "faces.csv",
        FACE_COLS,
        [_face_row(1, f, fid, decision="skip") for f in c1]
        + [_face_row(2, c2[0], fid, person="Mum", decision="keep")]
        + [_face_row(2, c2[1], fid, decision="skip")],
    )
    _write_csv(
        workdir / "tags.csv", ["file_id", "tag", "source", "score", "suggestion", "decision"], []
    )
    captured = {}
    monkeypatch.setattr(eapply, "exiftool_available", lambda: True)
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", _fake_exiftool(captured))
    _run(eapply.cmd_enrich_apply, conn, workdir, dry_run=False)

    for f in c1:
        row = conn.execute("SELECT ignored, person_id FROM faces WHERE id=?", (f,)).fetchone()
        assert row["ignored"] == 1 and row["person_id"] is None  # gone for good, never named
    # the lone ejected face in the named cluster is NOT ignored -> can be re-clustered later
    assert conn.execute("SELECT ignored FROM faces WHERE id=?", (c2[1],)).fetchone()["ignored"] == 0
    assert conn.execute("SELECT person_id FROM faces WHERE id=?", (c2[0],)).fetchone()["person_id"]


def test_cluster_excludes_ignored_faces(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(6):  # two kept identities (HDBSCAN needs contrast to form clusters)
        _insert_face(conn, fid, which=0)
    for _ in range(6):
        _insert_face(conn, fid, which=300)
    for _ in range(6):  # a third identity the user marked "not interested"
        _insert_face(conn, fid, which=100)
    conn.commit()
    all_ids = [r["id"] for r in conn.execute("SELECT id FROM faces ORDER BY id")]
    ignored = all_ids[12:]
    for f in ignored:
        conn.execute("UPDATE faces SET ignored=1 WHERE id=?", (f,))
    conn.commit()

    _run(ecluster.cmd_enrich_cluster, conn, workdir)

    for f in ignored:
        assert (
            conn.execute("SELECT cluster_id FROM faces WHERE id=?", (f,)).fetchone()["cluster_id"]
            is None
        )  # ignored faces never get clustered
    live = [r["cluster_id"] for r in conn.execute("SELECT cluster_id FROM faces WHERE ignored=0")]
    assert len({lbl for lbl in live if lbl is not None}) == 2  # only the kept identities cluster


def test_review_excludes_ignored_faces(tmp_path):
    conn, workdir, lib, ids = _seed(tmp_path, n=1)
    fid = ids[0]
    for _ in range(5):
        _insert_face(conn, fid, which=0)
    conn.execute("UPDATE faces SET cluster_id=1, cluster_prob=0.9")
    ignored = conn.execute("SELECT MIN(id) m FROM faces").fetchone()["m"]
    conn.execute("UPDATE faces SET ignored=1 WHERE id=?", (ignored,))
    conn.commit()

    _run(ereview.cmd_enrich_review, conn, workdir)
    rows = list(csv.DictReader((workdir / "faces.csv").open(encoding="utf-8")))
    assert {int(r["face_id"]) for r in rows} and ignored not in {int(r["face_id"]) for r in rows}


# --------------------------------------------------------------------------- status


def test_status_runs(tmp_path, capsys):
    conn, workdir, lib, ids = _seed(tmp_path, n=2)
    _insert_face(conn, ids[0], which=0)
    conn.execute("INSERT INTO tags(file_id, tag, status) VALUES (?,?,?)", (ids[0], "beach", "auto"))
    conn.commit()
    _run(estatus.cmd_enrich_status, conn, workdir)
    out = capsys.readouterr().out.lower()
    assert "face" in out and "tag" in out
