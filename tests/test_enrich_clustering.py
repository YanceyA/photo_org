"""HDBSCAN clustering of face embeddings + nearest-person assignment (pure, no GPU)."""

import numpy as np

from photoflow.enrich.clustering import cluster_embeddings, nearest_person


def _blobs(rng, n_per=15, spread=0.04, dim=512):
    """Three well-separated blobs on the unit sphere + two isolated outliers.

    Random high-dim centers are ~orthogonal, so blobs are far apart in cosine space;
    a tight spread keeps each blob's angular radius small -> clean, deterministic clusters.
    """
    centers = rng.standard_normal((3, dim))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    pts, truth = [], []
    for c_idx, c in enumerate(centers):
        for _ in range(n_per):
            pts.append(c + rng.standard_normal(dim) * spread)
            truth.append(c_idx)
    for _ in range(2):  # isolated singletons -> noise (< min_cluster_size)
        pts.append(rng.standard_normal(dim))
        truth.append(-1)
    return np.array(pts, dtype=np.float32), truth


def test_cluster_embeddings_finds_three_people():
    rng = np.random.default_rng(7)
    embs, truth = _blobs(rng)
    labels, probs, medoids = cluster_embeddings(embs, min_cluster_size=5)

    non_noise = {int(lbl) for lbl in labels if lbl != -1}
    assert len(non_noise) == 3  # three distinct people

    # Every member of a true blob shares one cluster label.
    for blob in range(3):
        rows = [i for i, t in enumerate(truth) if t == blob]
        blob_labels = {int(labels[i]) for i in rows}
        assert len(blob_labels) == 1 and -1 not in blob_labels


def _two_clusters_one_with_submodes():
    """Two far-apart people, where the second is really two near sub-modes (e.g. one identity the
    clusterer lumped together). eom keeps that mega-cluster whole; leaf splits its sub-modes.
    Deterministic via a fixed seed."""
    rng = np.random.default_rng(0)

    def blob(dirs, n, spread=0.02):
        v = np.zeros((n, 512), dtype=np.float32)
        for d, val in dirs:
            v[:, d] = val
            v[:, d] += rng.normal(0, spread, n).astype(np.float32)
        v[:, 50:54] += rng.normal(0, spread, (n, 4)).astype(np.float32)  # realistic jitter
        return v

    person_x = blob([(0, 1.0)], 20)  # a distinct person, far from the rest
    y_mode_a = blob([(5, 1.0), (6, 0.0)], 20)  # person Y, sub-mode A
    y_mode_b = blob([(5, 1.0), (6, 0.12)], 20)  # person Y, sub-mode B (a small tilt away)
    return np.vstack([person_x, y_mode_a, y_mode_b])


def test_cluster_selection_method_leaf_splits_finer_than_eom():
    # The knob for breaking up a mega-cluster: leaf selection carves a lumped blob into more,
    # finer sub-clusters than the default eom (which prefers one big stable parent).
    embs = _two_clusters_one_with_submodes()
    eom_labels, _, _ = cluster_embeddings(embs, min_cluster_size=8, cluster_selection_method="eom")
    leaf_labels, _, _ = cluster_embeddings(
        embs, min_cluster_size=8, cluster_selection_method="leaf"
    )
    n_eom = len({int(lbl) for lbl in eom_labels if lbl != -1})
    n_leaf = len({int(lbl) for lbl in leaf_labels if lbl != -1})
    assert n_leaf > n_eom  # leaf pulls out the stragglers eom buried in the parent


def test_cluster_probabilities_in_unit_range():
    rng = np.random.default_rng(3)
    embs, _ = _blobs(rng)
    _, probs, _ = cluster_embeddings(embs, min_cluster_size=5)
    assert probs.shape[0] == embs.shape[0]
    assert float(probs.min()) >= 0.0 and float(probs.max()) <= 1.0


def test_medoid_index_is_a_real_cluster_member():
    rng = np.random.default_rng(11)
    embs, _ = _blobs(rng)
    labels, _, medoids = cluster_embeddings(embs, min_cluster_size=5)
    for label, row in medoids.items():
        assert label != -1
        assert int(labels[row]) == label  # the medoid is itself a member of its cluster


def test_nearest_person_matches_close_rejects_far():
    rng = np.random.default_rng(5)
    a = rng.standard_normal(512).astype(np.float32)
    a /= np.linalg.norm(a)
    b = rng.standard_normal(512).astype(np.float32)
    b /= np.linalg.norm(b)
    centroids = {10: a, 20: b}

    near_a = a + rng.standard_normal(512).astype(np.float32) * 0.02
    pid, sim = nearest_person(near_a, centroids, threshold=0.5)
    assert pid == 10 and sim >= 0.5

    far = rng.standard_normal(512).astype(np.float32)
    far /= np.linalg.norm(far)
    pid2, sim2 = nearest_person(far, centroids, threshold=0.5)
    assert pid2 is None


def test_nearest_person_empty_pool():
    q = np.ones(512, dtype=np.float32)
    assert nearest_person(q, {}, threshold=0.5) == (None, 0.0)


def test_cluster_empty_input():
    labels, probs, medoids = cluster_embeddings(np.zeros((0, 512), dtype=np.float32))
    assert labels.shape == (0,) and probs.shape == (0,) and medoids == {}
