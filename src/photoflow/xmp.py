"""XMP provenance: sidecar files for non-embeddable formats, argfile lines for the rest."""

from __future__ import annotations

import html
from pathlib import Path

EMBED_EXT = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"}


def xmp_sidecar(dest: Path, description: str, keywords: list[str]):
    kw = "".join(f"<rdf:li>{html.escape(k)}</rdf:li>" for k in keywords)
    xml = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{html.escape(description)}</rdf:li></rdf:Alt></dc:description>
   <dc:subject><rdf:Bag>{kw}</rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    dest.with_suffix(dest.suffix + ".xmp").write_text(xml, encoding="utf-8")


def embed_args(dest: str, description: str, keywords: list[str]) -> list[str]:
    """exiftool argfile lines to embed Dublin Core XMP into one file.

    -P preserves FileModifyDate: without it -overwrite_original resets the library
    file's mtime to "now", breaking HANDOFF §2.1 and re-triggering mtime-based
    re-indexing (Immich) / re-upload (backup) of the whole library.
    """
    lines = ["-P", "-overwrite_original", f"-XMP-dc:Description={description}"]
    lines += [f"-XMP-dc:Subject={k}" for k in keywords]
    lines += [dest, "-execute"]
    return lines
