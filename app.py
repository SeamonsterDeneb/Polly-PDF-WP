import io
import json
import re
import fitz  # PyMuPDF
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# POLLY PDF CORE — Alt Text Injection Engine
# ---------------------------------------------------------------------------
# Strategy:
#   For TAGGED PDFs (have /StructTreeRoot):
#     - Rewrite the page content stream to wrap the target image's q...Do...Q
#       block in a /Figure <</MCID N>> BDC ... EMC pair (converting it from
#       /Artifact to a proper tagged Figure).
#     - Create a new /Figure struct element object with /Alt text.
#     - Append it to the document-level struct element's /K array.
#     - Add the new MCID -> struct element mapping to the ParentTree.
#     - Increment /ParentTreeNextKey.
#
#   For UNTAGGED PDFs (no /StructTreeRoot):
#     - Falls back to injecting /Alt directly on the image XObject dictionary.
#       This is less robust but gives screen readers something to work with
#       until full tagging support is added for untagged docs.
#
# Input (multipart/form-data):
#   pdf      — the PDF file
#   metadata — JSON string: { "0": { "pageIdx": 5, "imgIdx": 0, "alt": "..." }, ... }
#              pageIdx: 0-based page number
#              imgIdx:  0-based index of raster image on that page (as returned
#                       by fitz page.get_images())
#              alt:     the alt text string to inject
# ---------------------------------------------------------------------------


def is_tagged(doc: fitz.Document) -> bool:
    """Return True if the PDF has a StructTreeRoot (is a tagged PDF)."""
    catalog = doc.xref_object(doc.pdf_catalog())
    return "/StructTreeRoot" in catalog


def get_struct_tree_next_key(doc: fitz.Document) -> int:
    """Read /ParentTreeNextKey from the StructTreeRoot."""
    struct_root_xref = _get_struct_root_xref(doc)
    obj = doc.xref_object(struct_root_xref)
    m = re.search(r"/ParentTreeNextKey\s+(\d+)", obj)
    if not m:
        raise ValueError("Could not find /ParentTreeNextKey in StructTreeRoot")
    return int(m.group(1))


def _get_struct_root_xref(doc: fitz.Document) -> int:
    catalog = doc.xref_object(doc.pdf_catalog())
    m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", catalog)
    if not m:
        raise ValueError("No /StructTreeRoot found in catalog")
    return int(m.group(1))


def _get_parent_tree_xref(doc: fitz.Document) -> int:
    struct_root = doc.xref_object(_get_struct_root_xref(doc))
    m = re.search(r"/ParentTree\s+(\d+)\s+0\s+R", struct_root)
    if not m:
        raise ValueError("No /ParentTree found in StructTreeRoot")
    return int(m.group(1))


def _get_document_struct_xref(doc: fitz.Document) -> int:
    """Find the top-level /Document struct element (direct child of StructTreeRoot)."""
    struct_root = doc.xref_object(_get_struct_root_xref(doc))
    m = re.search(r"/K\s+(\d+)\s+0\s+R", struct_root)
    if m:
        return int(m.group(1))
    raise ValueError("Could not find document-level struct element")


def _get_contents_xref(doc: fitz.Document, page_num: int) -> int:
    """Get the xref of the page's content stream."""
    page = doc[page_num]
    obj = doc.xref_object(page.xref)
    m = re.search(r"/Contents\s+(\d+)\s+0\s+R", obj)
    if m:
        return int(m.group(1))
    raise ValueError(f"Page {page_num+1} has no single /Contents stream (may be array — not yet supported)")


def rewrite_content_stream_for_image(
    doc: fitz.Document, page_num: int, img_index: int, mcid: int
) -> bool:
    """
    Find the nth raster image draw sequence on the page and wrap it in a
    /Figure BDC ... EMC block with the given MCID.

    The pattern we target is the innermost save/restore block:
        \\nq\\n/GS<n> gs\\n<matrix> cm\\n/Im<n> Do\\nQ
    which InDesign generates for every raster image placement.
    """
    page = doc[page_num]
    content = page.read_contents().decode("latin-1")

    # Match the innermost q.../ImN Do/Q image block
    img_block_pattern = re.compile(
        r"\nq\n/GS\d+ gs\n[^\n]+ cm\n/Im\d+ Do\nQ"
    )
    matches = list(img_block_pattern.finditer(content))

    if img_index >= len(matches):
        raise IndexError(
            f"imgIdx {img_index} out of range: page {page_num+1} has "
            f"{len(matches)} raster image(s)"
        )

    match = matches[img_index]
    old_block = match.group(0)
    new_block = f"\n/Figure <</MCID {mcid}>>BDC{old_block}\nEMC"

    new_content = content[: match.start()] + new_block + content[match.end() :]
    contents_xref = _get_contents_xref(doc, page_num)
    doc.update_stream(contents_xref, new_content.encode("latin-1"))
    return True


def create_figure_struct_element(
    doc: fitz.Document, page_xref: int, mcid: int, alt_text: str
) -> int:
    """
    Allocate a new xref and write a /Figure struct element to it.
    Returns the new xref number.
    """
    doc_struct_xref = _get_document_struct_xref(doc)
    new_xref = doc.get_new_xref()

    # Escape parentheses in alt text for PDF string syntax
    safe_alt = alt_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    struct_obj = (
        f"<</S /Figure "
        f"/Alt ({safe_alt}\\000) "
        f"/K {mcid} "
        f"/P {doc_struct_xref} 0 R "
        f"/Pg {page_xref} 0 R "
        f"/T () >>"
    )
    doc.update_object(new_xref, struct_obj)
    return new_xref


def append_to_document_struct_k(doc: fitz.Document, new_xref: int) -> None:
    """Add the new struct element xref to the document-level /K array."""
    doc_struct_xref = _get_document_struct_xref(doc)
    doc_struct = doc.xref_object(doc_struct_xref)

    new_ref = f"{new_xref} 0 R"
    updated = re.sub(
        r"(/K\s*\[)([^\]]+)(\])",
        lambda m: m.group(1) + m.group(2) + f"\n      {new_ref} " + m.group(3),
        doc_struct,
    )

    if updated == doc_struct:
        raise ValueError("Could not update /K array in document struct element")

    doc.update_object(doc_struct_xref, updated)


def update_parent_tree(doc: fitz.Document, mcid: int, struct_xref: int) -> None:
    """Add the new MCID -> struct element mapping to the ParentTree /Nums array."""
    pt_xref = _get_parent_tree_xref(doc)
    pt_obj = doc.xref_object(pt_xref)

    new_entry = f"\n      {mcid} {struct_xref} 0 R"
    updated = re.sub(
        r"(\]\s*\n>>)",
        lambda m: new_entry + " " + m.group(1),
        pt_obj,
    )

    if updated == pt_obj:
        raise ValueError("Could not insert new entry into ParentTree /Nums array")

    doc.update_object(pt_xref, updated)


def increment_parent_tree_next_key(doc: fitz.Document, used_mcid: int) -> None:
    """Bump /ParentTreeNextKey by 1."""
    struct_root_xref = _get_struct_root_xref(doc)
    struct_root = doc.xref_object(struct_root_xref)
    updated = re.sub(
        r"/ParentTreeNextKey\s+\d+",
        f"/ParentTreeNextKey {used_mcid + 1}",
        struct_root,
    )
    doc.update_object(struct_root_xref, updated)


def inject_alt_tagged(doc: fitz.Document, page_num: int, img_index: int, alt_text: str) -> str:
    """
    Full tagged-PDF remediation pipeline for one image.
    Returns a status string for logging.
    """
    mcid = get_struct_tree_next_key(doc)
    page_xref = doc[page_num].xref

    rewrite_content_stream_for_image(doc, page_num, img_index, mcid)
    fig_xref = create_figure_struct_element(doc, page_xref, mcid, alt_text)
    append_to_document_struct_k(doc, fig_xref)
    update_parent_tree(doc, mcid, fig_xref)
    increment_parent_tree_next_key(doc, mcid)

    return (
        f"Tagged: page {page_num+1}, imgIdx {img_index} "
        f"-> MCID {mcid}, struct xref {fig_xref}"
    )


def inject_alt_untagged(doc: fitz.Document, page_num: int, img_index: int, alt_text: str) -> str:
    """
    Fallback for untagged PDFs: write /Alt onto the image XObject dict.
    Does NOT touch the pixel data stream, only the dictionary envelope.
    """
    page = doc[page_num]
    images = page.get_images(full=True)

    if img_index >= len(images):
        raise IndexError(
            f"imgIdx {img_index} out of range: page {page_num+1} has "
            f"{len(images)} image(s)"
        )

    img_xref = images[img_index][0]
    img_obj = doc.xref_object(img_xref)

    safe_alt = alt_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    if "/Alt" in img_obj:
        updated = re.sub(r"/Alt\s*\([^)]*\)", f"/Alt ({safe_alt})", img_obj)
    else:
        updated = img_obj.rstrip(">").rstrip() + f"\n  /Alt ({safe_alt})\n>>"

    doc.update_object(img_xref, updated)
    return f"Untagged fallback: page {page_num+1}, imgIdx {img_index}, xref {img_xref}"


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

@app.route("/remediate", methods=["POST"])
def remediate_pdf():
    try:
        # --- Validate request ---
        if "pdf" not in request.files:
            return jsonify({"error": "No PDF file in request"}), 400

        metadata_str = request.form.get("metadata", "{}")
        remediation_map = json.loads(metadata_str)

        if not remediation_map:
            return jsonify({"error": "metadata is empty"}), 400

        # --- Load PDF ---
        file_bytes = request.files["pdf"].read()
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        tagged = is_tagged(doc)

        print(f"🦜 [Polly Core] PDF loaded — tagged: {tagged}, pages: {doc.page_count}")

        results = []
        errors = []

        # --- Process each remediation entry ---
        for asset_id, data in remediation_map.items():
            page_num = int(data.get("pageIdx", 0))
            img_index = int(data.get("imgIdx", 0))
            alt_text = data.get("alt", "").strip()

            if not alt_text:
                errors.append(f"Asset {asset_id}: empty alt text, skipped")
                continue

            if page_num < 0 or page_num >= doc.page_count:
                errors.append(f"Asset {asset_id}: pageIdx {page_num} out of range")
                continue

            try:
                if tagged:
                    status = inject_alt_tagged(doc, page_num, img_index, alt_text)
                else:
                    status = inject_alt_untagged(doc, page_num, img_index, alt_text)

                results.append(status)
                print(f"🦜 [Polly Core] ✓ {status}")

            except (IndexError, ValueError) as e:
                msg = f"Asset {asset_id}: {e}"
                errors.append(msg)
                print(f"🦜 [Polly Core] ✗ {msg}")

        if not results and errors:
            return jsonify({"error": "All remediations failed", "details": errors}), 422

        # --- Compile output ---
        output_buffer = io.BytesIO()
        doc.save(output_buffer, garbage=3, deflate=True)
        doc.close()
        output_buffer.seek(0)

        print(f"🦜 [Polly Core] Done. {len(results)} injected, {len(errors)} skipped.")

        return send_file(
            output_buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="remediated.pdf",
        )

    except Exception as e:
        print(f"❌ [Polly Core] Crash: {e}")
        return jsonify({"error": f"Internal error: {e}"}), 500


@app.route("/inspect", methods=["POST"])
def inspect_pdf():
    """
    Helper endpoint: given a PDF, return the list of raster images per page
    so the WordPress frontend can map PDF.js canvas tokens to (pageIdx, imgIdx) pairs.

    Returns JSON:
    {
      "tagged": true,
      "pages": {
        "0": [{ "imgIdx": 0, "xref": 363, "width": 2560, "height": 1853 }],
        ...
      }
    }
    """
    try:
        if "pdf" not in request.files:
            return jsonify({"error": "No PDF file"}), 400

        file_bytes = request.files["pdf"].read()
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        tagged = is_tagged(doc)

        pages = {}
        for page_num in range(doc.page_count):
            images = doc[page_num].get_images(full=True)
            if images:
                pages[str(page_num)] = [
                    {
                        "imgIdx": i,
                        "xref": img[0],
                        "width": img[2],
                        "height": img[3],
                        "name": img[7],
                    }
                    for i, img in enumerate(images)
                ]

        doc.close()
        return jsonify({"tagged": tagged, "pages": pages})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("🦜 Polly PDF Microserver active on port 5001...")
    print("   POST /remediate  — inject alt text into images")
    print("   POST /inspect    — list raster images by page for frontend mapping")
    app.run(host="0.0.0.0", port=5001, debug=True)