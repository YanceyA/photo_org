"""Integration tests for the enrich command modules.

Models are faked (insightface/torch aren't installed); clustering uses real scikit-learn;
the XMP writeback is captured via a monkeypatched exiftool runner, with one real-exiftool
round-trip marked @pytest.mark.exiftool.
"""

import csv
import types
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
from photoflow.enrich import review as ereview
from photoflow.enrich import scan as escan
from photoflow.enrich import status as estatus
from photoflow.enrich import tagger as etagger


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


def _run(fn, conn, workdir, **argkw):
    run_id = new_run(conn, "enrich", {})
    args = types.SimpleNamespace(**argkw)
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id}.jsonl", "a", encoding="utf-8") as log_fh:
        fn(conn, workdir, run_id, log_fh, args, Config())


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


def test_cluster_passes_selection_epsilon_from_config(tmp_path, monkeypatch):
    # Layer 1 knob: cluster_selection_epsilon merges adjacent burst-fragment clusters of one
    # person. cluster_embeddings already accepts it; this guards that the command threads the
    # configured value through (default 0.0 keeps today's behavior).
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
    cfg = replace(Config(), enrich_cluster_selection_epsilon=0.35)
    run_id = new_run(conn, "enrich", {})
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"run_{run_id}.jsonl", "a", encoding="utf-8") as fh:
        ecluster.cmd_enrich_cluster(conn, workdir, run_id, fh, types.SimpleNamespace(), cfg)
    assert captured["cluster_selection_epsilon"] == 0.35


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
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(
        eapply, "exiftool_apply_argfile", lambda lines: captured.setdefault("lines", lines)
    )

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
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(
        eapply, "exiftool_apply_argfile", lambda lines: captured.setdefault("lines", lines)
    )
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
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(
        eapply, "exiftool_apply_argfile", lambda lines: captured.setdefault("lines", lines)
    )
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
    monkeypatch.setattr(eapply, "read_keywords", lambda paths: {p: set() for p in paths})
    monkeypatch.setattr(eapply, "exiftool_apply_argfile", lambda lines: None)
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
