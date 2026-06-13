"""photoflow enrich subsystem: faces (InsightFace) + content tags (RAM++/CLIP).

Pure, testable logic (clustering, regions, page, thresholds) lives here alongside the
heavy model wrappers (faces, tagger), which lazy-import their optional dependencies.
"""
