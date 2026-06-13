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
