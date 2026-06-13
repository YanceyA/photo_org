"""Pure clustering of face embeddings into per-person groups.

Depends only on numpy + scikit-learn (sklearn.cluster.HDBSCAN, built into sklearn >=1.3 -
no separate `hdbscan` package). No GPU, no I/O: unit-testable with synthetic embeddings.

InsightFace ArcFace embeddings are clustered by L2-normalizing then running HDBSCAN with
the default euclidean metric: on the unit sphere euclidean distance is monotonic in cosine
distance, so this is cosine-equivalent while staying on HDBSCAN's fast tree path
(metric='cosine' would force the slow brute-force path with no quality gain).
"""

from __future__ import annotations

import numpy as np


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.clip(norms, 1e-12, None)


def cluster_embeddings(
    embeddings: np.ndarray,
    *,
    min_cluster_size: int = 5,
    min_samples: int | None = None,
    cluster_selection_epsilon: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, dict[int, int]]:
    """Cluster (N, D) embeddings into people.

    Returns:
        labels: (N,) int array; -1 marks noise / unassigned faces.
        probabilities: (N,) float array in [0, 1]; low values flag edge-case members.
        medoids: {cluster_label: row_index} - the representative (real, observed) face per
            cluster, suitable to show as the cluster's sample thumbnail.
    """
    embeddings = np.asarray(embeddings, dtype=np.float32)
    n = embeddings.shape[0]
    if n == 0:
        return np.empty(0, dtype=int), np.empty(0, dtype=float), {}
    if n < min_cluster_size:
        # HDBSCAN needs at least min_cluster_size points to form any cluster.
        return np.full(n, -1, dtype=int), np.zeros(n, dtype=float), {}

    from sklearn.cluster import HDBSCAN

    x = _l2_normalize(embeddings)
    clu = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",  # cosine-equivalent on unit vectors, stays on the fast path
        cluster_selection_method="eom",  # fewer/larger stable clusters (one-per-person)
        cluster_selection_epsilon=cluster_selection_epsilon,
        store_centers="medoid",  # medoids are real observed faces, safe to display
        copy=True,  # don't mutate caller's array; also silences the 1.9->1.10 FutureWarning
        n_jobs=-1,
    ).fit(x)

    labels = clu.labels_.astype(int)
    probs = clu.probabilities_.astype(float)

    medoids: dict[int, int] = {}
    for label in sorted(set(labels)):
        if label == -1:
            continue
        med = clu.medoids_[label]  # shape (D,); an observed point
        medoids[label] = int(np.argmin(np.linalg.norm(x - med, axis=1)))
    return labels, probs, medoids


def nearest_person(
    embedding: np.ndarray,
    person_centroids: dict[int, np.ndarray],
    threshold: float,
) -> tuple[int | None, float]:
    """Cosine-nearest existing person to a single embedding.

    Used to pre-suggest a name for a freshly scanned face that lands near an already-named
    person, so incremental imports don't have to re-cluster everyone. Returns
    (person_id, similarity) when the best match is >= threshold, else (None, best_sim).
    """
    if not person_centroids:
        return None, 0.0
    q = np.asarray(embedding, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    best_id, best_sim = None, -1.0
    for pid, centroid in person_centroids.items():
        c = np.asarray(centroid, dtype=np.float32)
        c = c / max(float(np.linalg.norm(c)), 1e-12)
        sim = float(np.dot(q, c))
        if sim > best_sim:
            best_id, best_sim = pid, sim
    if best_sim >= threshold:
        return best_id, best_sim
    return None, max(best_sim, 0.0)
