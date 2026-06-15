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
    """
    Read /ParentTreeNextKey from the StructTreeRoot.
    Falls back to scanning the page content streams for the highest
    existing /MCID value when the key is absent (Acrobat/LibreOffice).
    """
    struct_root_xref = _get_struct_root_xref(doc)
    obj = doc.xref_object(struct_root_xref)

    # Fast path: InDesign and well-formed PDFs have this key explicitly
    m = re.search(r"/ParentTreeNextKey\s+(\d+)", obj)
    if m:
        return int(m.group(1))

    # Fallback: scan ALL page content streams for the highest /MCID in use
    # ParentTree Nums keys are PAGE SLOT indices, not MCIDs — don't use them
    print("🦜 [Polly Core] /ParentTreeNextKey absent — computing from content stream MCIDs")
    highest = -1
    for page_num in range(doc.page_count):
        try:
            content = doc[page_num].read_contents().decode("latin-1", errors="ignore")
            for mcid_val in re.findall(r"/MCID\s+(\d+)", content):
                highest = max(highest, int(mcid_val))
        except Exception:
            pass
    return highest + 1  # returns 0 if no MCIDs found, which is correct for a blank doc


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


def get_page_structparents(doc: fitz.Document, page_num: int) -> int:
    page_obj = doc.xref_object(doc[page_num].xref)

    m = re.search(r"/StructParents\s+(\d+)", page_obj)
    if not m:
        raise ValueError(
            f"Page {page_num+1} has no /StructParents entry"
        )

    return int(m.group(1))


def get_parenttree_array_xref(doc: fitz.Document, structparents: int) -> int:
    pt_xref = _get_parent_tree_xref(doc)
    pt_obj = doc.xref_object(pt_xref)

    pattern = rf"{structparents}\s+(\d+)\s+0\s+R"
    m = re.search(pattern, pt_obj)

    if not m:
        raise ValueError(
            f"ParentTree slot {structparents} not found"
        )

    return int(m.group(1))


def get_next_page_mcid(doc: fitz.Document, page_num: int) -> int:
    content = doc[page_num].read_contents().decode("latin-1", errors="ignore")

    mcids = [
        int(x)
        for x in re.findall(r"/MCID\s+(\d+)", content)
    ]

    if not mcids:
        return 0

    return max(mcids) + 1


def _get_document_struct_xref(doc: fitz.Document) -> int:
    """
    Find the element whose /K array we should append new struct elements to.
    - If StructTreeRoot's /K is a single ref to a /Document element, return that.
    - If StructTreeRoot's /K is already a flat array (no /Document wrapper),
      return the StructTreeRoot xref itself so we append directly to it.
    """
    struct_root_xref = _get_struct_root_xref(doc)
    struct_root = doc.xref_object(struct_root_xref)

    # Case 1: single /K ref -> check if it's a /Document element
    single = re.search(r'/K\s+(\d+)\s+0\s+R', struct_root)
    if single:
        candidate_xref = int(single.group(1))
        candidate = doc.xref_object(candidate_xref)
        if '/S /Document' in candidate or '/Document' in candidate:
            return candidate_xref

    # Case 2: /K is already a flat array on the StructTreeRoot itself
    return struct_root_xref


def _get_contents_xref(doc: fitz.Document, page_num: int) -> int:
    """
    Get the xref of the page's content stream, consolidating an array of
    streams into a single stream first if necessary (common in Acrobat/
    LibreOffice PDFs).
    """
    page = doc[page_num]
    obj = doc.xref_object(page.xref)

    # Single stream reference — the easy case
    m = re.search(r"/Contents\s+(\d+)\s+0\s+R", obj)
    if m:
        candidate = int(m.group(1))
        try:
            doc.xref_stream(candidate)   # throws if not a stream
            return candidate
        except Exception:
            pass  # fall through to array handling

    # Array case: /Contents [ 12 0 R  13 0 R ... ]
    array_m = re.search(r"/Contents\s*\[([^\]]+)\]", obj)
    if array_m:
        refs = [int(x) for x in re.findall(r"(\d+)\s+0\s+R", array_m.group(1))]
        if not refs:
            raise ValueError(f"Page {page_num+1} has an empty /Contents array")

        if len(refs) == 1:
            return refs[0]

        print(f"🦜 [Polly Core] Merging {len(refs)} content streams on page {page_num+1}")
        merged = b""
        for xref in refs:
            chunk = doc.xref_stream(xref)
            if chunk:
                if merged and not merged.endswith(b"\n"):
                    merged += b"\n"
                merged += chunk

        first_xref = refs[0]
        doc.update_stream(first_xref, merged)

        new_obj = re.sub(
            r"/Contents\s*\[[^\]]+\]",
            f"/Contents {first_xref} 0 R",
            obj,
        )
        doc.update_object(page.xref, new_obj)

        return first_xref

    raise ValueError(
        f"Page {page_num+1} has no recognisable /Contents entry "
        f"(neither a single ref nor an array)"
    )


def rewrite_content_stream_by_name_or_index(
    doc: fitz.Document, page_num: int, img_index: int, img_name: str, mcid: int
) -> bool:
    """
    Finds the drawing sequence of an image using either its explicit internal resource 
    dictionary key (e.g. /X0, /Im1, /X9) or a sequential fallback index, 
    then wraps it safely within a /Figure structural element tag.
    """
    print(f"🦜 [Polly Core] rewrite_content_stream: page={page_num} imgIdx={img_index} mcid={mcid}")
    page = doc[page_num]
    content = page.read_contents().decode("latin-1")
    print(f"🦜 [Polly Core] content stream length: {len(content)}")
    print(f"🦜 [Polly Core] Do commands found: {re.findall(r'/\\w+\\s+Do', content)}")

    # Clean the name entry to handle formatting variations
    clean_name = img_name.strip()
    if clean_name and not clean_name.startswith("/"):
        clean_name = "/" + clean_name

    # Try matching explicitly by the layout resource name
    do_pos = -1
    if clean_name:
        name_match = re.search(re.escape(clean_name) + r'\s+Do\b', content)
        if name_match:
            do_pos = name_match.start()

    # Fall back to index-based match
    if do_pos == -1:
        do_matches = list(re.finditer(r'/\w+\s+Do', content))
        print(f"🦜 [Polly Core] All Do commands: {[m.group(0) for m in do_matches]}")
        if img_index < len(do_matches):
            do_pos = do_matches[img_index].start()
        else:
            do_matches_fallback = list(re.finditer(r'/Im\d+\s+Do', content))
            if img_index < len(do_matches_fallback):
                do_pos = do_matches_fallback[img_index].start()

    print(f"🦜 [Polly Core] do_pos={do_pos}")

    if do_pos == -1:
        raise ValueError(f"Could not locate image target draw token on page {page_num+1}")

    pre = content[:do_pos]
    
    # Locate the outermost wrapping block boundaries
    bdc_match = None
    for m in re.finditer(r'/(Artifact|Figure|P|Span)\s*<<[^>]*>>\s*BDC', pre):
        bdc_match = m
    
    if bdc_match is None:
        # Search for q with any surrounding whitespace (inline or newline-separated)
        q_match = re.search(r'[\n ]q[\n ]', pre)
        # Walk backwards to find the LAST q before the Do command
        q_pos = -1
        for qm in re.finditer(r'(?:^|\n| )q(?:\n| )', pre):
            q_pos = qm.start()

        if q_pos < 0:
            # No q found at all — minimal wrap of just the Do command line
            post_line = content[do_pos:]
            end_line_match = re.search(r'[\n]|Q', post_line)
            end_line_pos = do_pos + (end_line_match.end() if end_line_match else len(post_line))
            target_draw_cmd = content[do_pos:end_line_pos].strip()
            new_content = (
                content[:do_pos]
                + f"\n/Figure << /MCID {mcid} >>BDC\n{target_draw_cmd}\nEMC\n"
                + content[end_line_pos:]
            )
            contents_xref = _get_contents_xref(doc, page_num)
            print(f"🦜 [Polly Core] contents_xref={contents_xref}, new content length={len(new_content)}")
            print(f"🦜 [Polly Core] content around injection: {repr(new_content[max(0,new_content.find('Figure')-20):new_content.find('Figure')+80])}")
            doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
            return True

        # Find the matching Q after the Do command
        # Use the full inline pattern — Q may be space-separated, not newline
        post = content[do_pos:]
        q_close = re.search(r'[\n ]Q[\n ]|[\n ]Q$', post)
        if not q_close:
            # Last resort — find any Q
            q_close = re.search(r'Q', post)
        if not q_close:
            raise ValueError(f"Could not find closing 'Q' frame after image on page {page_num+1}")

        close_pos = do_pos + q_close.end()

        # Grab the full q...Q block including the opening q whitespace char
        # q_pos points at the whitespace before q — include from q itself
        q_char_pos = q_pos + 1 if content[q_pos] in (' ', '\n') else q_pos
        img_block = content[q_char_pos:close_pos]
        new_block = f"\n/Figure << /MCID {mcid} >>BDC\n{img_block.strip()}\nEMC\n"
        new_content = content[:q_char_pos] + new_block + content[close_pos:]
    else:
        block_start = bdc_match.start()
        between = content[block_start:do_pos]
        open_count = len(re.findall(r'\bBDC\b', between))
        
        post = content[do_pos:]
        emc_positions = [m.end() for m in re.finditer(r'\bEMC\b', post)]
        
        if open_count > len(emc_positions):
            open_count = len(emc_positions)
        
        close_pos = do_pos + emc_positions[open_count - 1] if emc_positions else do_pos
        inner = content[block_start:close_pos]
        
        inner_q = re.search(r'q\s+.*Do.*\s+Q|q\n.*\nQ', inner, re.DOTALL)
        drawing = inner_q.group(0) if inner_q else inner
        
        new_block = f"\n/Figure << /MCID {mcid} >>BDC\n{drawing.strip()}\nEMC\n"
        new_content = content[:block_start] + new_block + content[close_pos:]

    contents_xref = _get_contents_xref(doc, page_num)
    print(f"🦜 [Polly Core] contents_xref={contents_xref}, new content length={len(new_content)}")
    print(f"🦜 [Polly Core] content around injection: {repr(new_content[max(0,new_content.find('Figure')-20):new_content.find('Figure')+80])}")
    doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
    return True


def create_figure_struct_element(
    doc: fitz.Document, page_xref: int, mcid: int, alt_text: str, page_num: int, img_index: int
) -> int:
    doc_struct_xref = _get_document_struct_xref(doc)

    try:
        img_infos = doc[page_num].get_image_info(hashes=False)
        raster_infos = [i for i in img_infos if i.get('width', 0) > 1]
        if img_index < len(raster_infos):
            bbox = raster_infos[img_index]['bbox']
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
        else:
            bbox = (0, 0, 612, 792)
            width, height = 612, 792
    except Exception:
        bbox = (0, 0, 612, 792)
        width, height = 612, 792

    safe_alt = alt_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # Inline the BBox array directly — do NOT use a separate xref for it.
    # PyMuPDF's update_object cannot create a valid indirect array object,
    # which is what was causing the code=7 crash on save.
    bbox_inline = f"[ {bbox[0]:.4f} {bbox[1]:.4f} {bbox[2]:.4f} {bbox[3]:.4f} ]"

    attr_xref = doc.get_new_xref()
    attr_obj = (
        f"<</O /Layout "
        f"/BBox {bbox_inline} "
        f"/Width {width:.4f} "
        f"/Height {height:.4f} >>"
    )
    doc.update_object(attr_xref, attr_obj)

    fig_xref = doc.get_new_xref()
    struct_obj = (
        f"<</S /Figure "
        f"/Alt ({safe_alt}\\000) "
        f"/K {mcid} "
        f"/A {attr_xref} 0 R "
        f"/P {doc_struct_xref} 0 R "
        f"/Pg {page_xref} 0 R "
        f"/T () >>"
    )
    doc.update_object(fig_xref, struct_obj)
    return fig_xref


def append_to_document_struct_k(doc: fitz.Document, new_xref: int) -> None:
    """Add the new struct element xref to the appropriate /K array."""
    target_xref = _get_document_struct_xref(doc)
    target_obj = doc.xref_object(target_xref)

    new_ref = f"{new_xref} 0 R"
    updated = re.sub(
        r"(/K\s*\[)([^\]]+)(\])",
        lambda m: m.group(1) + m.group(2) + f"\n      {new_ref} " + m.group(3),
        target_obj,
    )

    if updated == target_obj:
        raise ValueError("Could not update /K array in struct element")

    doc.update_object(target_xref, updated)


def update_parent_tree(doc: fitz.Document, mcid: int, struct_xref: int) -> None:
    """
    Add a new MCID->struct_xref mapping to the ParentTree.

    The ParentTree /Nums array maps PAGE SLOT indices (the /StructParents
    value on each page dict) to arrays of struct element refs — one ref
    per MCID on that page, in MCID order.

    For Acrobat/LibreOffice docs the new Figure's MCID may not be
    contiguous with the existing ones, so we append to the existing
    page-slot array rather than adding a new top-level entry.
    """
    pt_xref = _get_parent_tree_xref(doc)
    pt_obj = doc.xref_object(pt_xref)

    # Find the page-slot array that already exists for slot 0
    # (for single-page docs this is always slot 0; multi-page handled below)
    # Pattern: 0 [ ref ref ref ... ]
    slot_array_m = re.search(r'(0\s+\[)([^\]]*?)(\])', pt_obj)
    if slot_array_m:
        # Append new struct ref to the existing array for this page slot
        existing_entries = slot_array_m.group(2)
        new_entries = existing_entries.rstrip() + f"\n        {struct_xref} 0 R "
        updated = (
            pt_obj[:slot_array_m.start(2)]
            + new_entries
            + pt_obj[slot_array_m.end(2):]
        )
        doc.update_object(pt_xref, updated)
        return

    # No existing array for slot 0 — fall back to appending a new Nums entry
    # (this is the InDesign path where each MCID gets its own top-level slot)
    new_entry = f"\n  {mcid} {struct_xref} 0 R"
    if pt_obj.rstrip().endswith("]"):
        updated = pt_obj.rstrip()[:-1] + new_entry + "\n]"
    else:
        updated = pt_obj.rstrip().rstrip(">").rstrip() + new_entry + "\n>>"
    doc.update_object(pt_xref, updated)


def increment_parent_tree_next_key(doc: fitz.Document, used_mcid: int) -> None:
    """
    Bump /ParentTreeNextKey by 1.  If the key was absent (Acrobat/LibreOffice),
    insert it so subsequent remediations on the same document find it.
    """
    struct_root_xref = _get_struct_root_xref(doc)
    struct_root = doc.xref_object(struct_root_xref)
    new_val = used_mcid + 1

    if "/ParentTreeNextKey" in struct_root:
        updated = re.sub(
            r"/ParentTreeNextKey\s+\d+",
            f"/ParentTreeNextKey {new_val}",
            struct_root,
        )
    else:
        # Insert the key before the closing >>
        updated = struct_root.rstrip().rstrip(">").rstrip() + \
                  f"\n  /ParentTreeNextKey {new_val}\n>>"

    doc.update_object(struct_root_xref, updated)


def inject_alt_tagged(doc: fitz.Document, page_num: int, img_index: int, alt_text: str, img_name: str = "") -> str:
    """
    Full tagged-PDF remediation pipeline for one image.
    Returns a status string for logging.
    """
    # Diagnostic: check content stream for Figure BDC markers
    content = doc[page_num].read_contents().decode("latin-1", errors="ignore")
    figure_bdcs = re.findall(r'/Figure\s*<<[^>]*/MCID\s+(\d+)[^>]*>>\s*BDC', content)
    print(f"🦜 [Polly Core] inject_alt_tagged: page={page_num} imgIdx={img_index} figure_bdcs_in_stream={figure_bdcs}")
    mcid = get_struct_tree_next_key(doc)
    page_xref = doc[page_num].xref

    rewrite_content_stream_by_name_or_index(doc, page_num, img_index, img_name, mcid)
    fig_xref = create_figure_struct_element(doc, page_xref, mcid, alt_text, page_num, img_index)
    append_to_document_struct_k(doc, fig_xref)
    update_parent_tree(doc, mcid, fig_xref)
    increment_parent_tree_next_key(doc, mcid)

    return (
        f"Tagged: page {page_num+1}, imgIdx {img_index} "
        f"-> MCID {mcid}, struct xref {fig_xref}"
    )

def _get_image_resource_name(doc: fitz.Document, page_num: int, img_xref: int) -> str:
    """
    Find the resource name (e.g. '/Im1') used to reference this image
    xref in the page's /Resources /XObject dict.
    """
    page_obj = doc.xref_object(doc[page_num].xref)
    res_m = re.search(r'/Resources\s+(\d+)\s+0\s+R', page_obj)
    if res_m:
        res_obj = doc.xref_object(int(res_m.group(1)))
    else:
        res_obj = page_obj

    xobj_m = re.search(r'/XObject\s*<<([^>]*)>>', res_obj)
    if xobj_m:
        for name, xref in re.findall(r'(/\w+)\s+(\d+)\s+0\s+R', xobj_m.group(1)):
            if int(xref) == img_xref:
                return name
    return ""


def _ensure_struct_tree(doc: fitz.Document) -> None:
    cat_xref = doc.pdf_catalog()
    cat_obj = doc.xref_object(cat_xref)

    if "/StructTreeRoot" in cat_obj:
        return

    print("🦜 [Polly Core] Building struct tree scaffold for untagged PDF")

    # 1. ParentTree
    pt_xref = doc.get_new_xref()
    print(f"🦜 [Polly Core] scaffold step 1: ParentTree xref={pt_xref}")
    doc.update_object(pt_xref, "<< /Nums [] >>")

    # 2. Document struct element
    doc_xref = doc.get_new_xref()
    print(f"🦜 [Polly Core] scaffold step 2: Document elem xref={doc_xref}")
    doc.update_object(doc_xref, "<< /Type /StructElem /S /Document /K [] >>")

    # 3. StructTreeRoot
    str_root_xref = doc.get_new_xref()
    print(f"🦜 [Polly Core] scaffold step 3: StructTreeRoot xref={str_root_xref}")
    str_root_obj = (
        f"<< /Type /StructTreeRoot "
        f"/ParentTree {pt_xref} 0 R "
        f"/ParentTreeNextKey 0 "
        f"/RoleMap << /Figure /Figure /H1 /H1 /H2 /H2 /P /P /L /L /LI /LI /LBody /LBody >> "
        f"/K [ {doc_xref} 0 R ] >>"
    )
    doc.update_object(str_root_xref, str_root_obj)

    # 4. Set /P on Document element
    print(f"🦜 [Polly Core] scaffold step 4: wiring /P on Document elem")
    doc.update_object(
        doc_xref,
        f"<< /Type /StructElem /S /Document "
        f"/P {str_root_xref} 0 R /K [] >>"
    )

    # 5. Add to catalog
    print(f"🦜 [Polly Core] scaffold step 5: updating catalog xref={cat_xref}")
    print(f"🦜 [Polly Core] catalog obj: {repr(cat_obj[:200])}")
    updated_cat = cat_obj.rstrip().rstrip(">").rstrip()
    updated_cat += (
        f"\n  /StructTreeRoot {str_root_xref} 0 R"
        f"\n  /MarkInfo << /Marked true >>\n>>"
    )
    doc.update_object(cat_xref, updated_cat)

    # 6. StructParents on pages
    print(f"🦜 [Polly Core] scaffold step 6: adding /StructParents to pages")
    for i in range(doc.page_count):
        page_obj = doc.xref_object(doc[i].xref)
        if "/StructParents" not in page_obj:
            updated_page = page_obj.rstrip().rstrip(">").rstrip()
            updated_page += f"\n  /StructParents {i}\n>>"
            doc.update_object(doc[i].xref, updated_page)

    print(f"🦜 [Polly Core] scaffold complete")

def inject_alt_untagged(doc: fitz.Document, page_num: int, img_index: int, alt_text: str) -> str:
    """
    For untagged PDFs: build a minimal struct tree if absent, then inject
    a /Figure element with /Alt for the target image.
    Falls back to a safe no-op rather than corrupting the image stream.
    """
    page = doc[page_num]
    images = page.get_images(full=True)
    raster = [i for i in images if i[2] > 1]  # filter by width > 1

    if img_index >= len(raster):
        raise IndexError(
            f"imgIdx {img_index} out of range: page {page_num+1} has "
            f"{len(raster)} raster image(s)"
        )

    img_xref = raster[img_index][0]
    safe_alt = alt_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # Ensure the document has a struct tree scaffold
    _ensure_struct_tree(doc)

    # Assign a new MCID for this image
    mcid = get_struct_tree_next_key(doc)

    # Wrap the image draw command in a Figure BDC/EMC in the content stream
    rewrite_content_stream_by_name_or_index(
        doc, page_num, img_index,
        _get_image_resource_name(doc, page_num, img_xref),
        mcid
    )

    # Create the Figure struct element
    page_xref = page.xref
    fig_xref = create_figure_struct_element(
        doc, page_xref, mcid, alt_text, page_num, img_index
    )

    # Wire it into the struct tree
    append_to_document_struct_k(doc, fig_xref)
    update_parent_tree(doc, mcid, fig_xref)
    increment_parent_tree_next_key(doc, mcid)

    return (
        f"Untagged+scaffold: page {page_num+1}, imgIdx {img_index}, "
        f"MCID {mcid}, struct xref {fig_xref}"
    )


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
            img_name = data.get("imgName", "").strip()  # Universal Phase 3 fallback
            alt_text = data.get("alt", "").strip()

            if not alt_text:
                errors.append(f"Asset {asset_id}: empty alt text, skipped")
                continue

            if page_num < 0 or page_num >= doc.page_count:
                errors.append(f"Asset {asset_id}: pageIdx {page_num} out of range")
                continue

            try:
                is_decorative = data.get("decorative", False)

                if is_decorative:
                    # Phase 1 stub: skip decorative images until full artifact
                    # pipeline is implemented in Phase 2
                    print(f"🦜 [Polly Core] ⬜ Asset {asset_id}: marked decorative — skipping (Phase 2 pending)")
                    results.append(f"Decorative (stub): page {page_num+1}, imgIdx {img_index}")
                    continue

                if tagged:
                    status = inject_alt_tagged(doc, page_num, img_index, alt_text, img_name)
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
        print(f"🦜 [Polly Core] Attempting save...")
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

def find_alt_in_struct_tree(doc: fitz.Document, page_num: int, img_index: int) -> str:
    """
    Look for existing /Alt text for an image in the struct tree.
    Checks both the image XObject dict (untagged path) and Figure struct
    elements (tagged path, used by Word/InDesign exports).
    Returns the alt string if found, empty string if not.
    """
    # Fast path: alt on the XObject dict itself
    try:
        images = doc[page_num].get_images(full=True)
        raster = [i for i in images if i[2] > 1]
        if img_index < len(raster):
            img_xref = raster[img_index][0]
            obj = doc.xref_object(img_xref)
            alt_m = re.search(r'/Alt\s*\(([^)]*)\)', obj)
            if alt_m:
                return alt_m.group(1)
    except Exception:
        pass

    # Struct tree path
    try:
        cat = doc.xref_object(doc.pdf_catalog())
        str_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat)
        if not str_m:
            return ""

        page_xref = doc[page_num].xref
        content = doc[page_num].read_contents().decode("latin-1", errors="ignore")

        # First try: match via /Figure BDC MCID markers in content stream
        figure_bdcs = list(re.finditer(
            r'/Figure\s*<<[^>]*/MCID\s+(\d+)[^>]*>>\s*BDC', content
        ))
        target_mcid = None
        if img_index < len(figure_bdcs):
            target_mcid = int(figure_bdcs[img_index].group(1))

        # Collect all Figure struct elements on this page in xref order
        page_figures = []
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref)
                if not obj or '/Figure' not in obj:
                    continue
                s_m = re.search(r'/S\s+(/\w+)', obj)
                if not s_m or s_m.group(1) != '/Figure':
                    continue
                pg_m = re.search(r'/Pg\s+(\d+)\s+0\s+R', obj)
                if not pg_m or int(pg_m.group(1)) != page_xref:
                    continue
                alt_m = re.search(r'/Alt\s*\(([^)]*)\)', obj)
                k_m = re.search(r'/K\s+\[?\s*(\d+)\s*\]?', obj)
                page_figures.append({
                    'xref': xref,
                    'mcid': int(k_m.group(1)) if k_m else -1,
                    'alt': alt_m.group(1) if alt_m else '',
                })
            except Exception:
                pass

        if not page_figures:
            return ""

        # If we found a target MCID via content stream, match on that
        if target_mcid is not None:
            for fig in page_figures:
                if fig['mcid'] == target_mcid:
                    return fig['alt']

        # Fallback: return the nth Figure element on this page by xref order
        # This handles Word/Quartz PDFs where content stream has no BDC markers
        if img_index < len(page_figures):
            return page_figures[img_index]['alt']

    except Exception:
        pass

    return ""
    
@app.route("/inspect", methods=["POST"])
def inspect_pdf():
    """
    Helper endpoint: given a PDF, return the list of raster images per page
    so the WordPress frontend can map PDF.js canvas tokens to (pageIdx, imgIdx) pairs.
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
            raster = [img for img in images if img[2] > 1]
            if raster:
                pages[str(page_num)] = [
                    {
                        "imgIdx": i,
                        "xref": img[0],
                        "width": img[2],
                        "height": img[3],
                        "name": img[7],
                        "existingAlt": find_alt_in_struct_tree(doc, page_num, i),
                    }
                    for i, img in enumerate(raster)
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