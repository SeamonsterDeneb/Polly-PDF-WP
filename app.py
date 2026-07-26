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
    - If StructTreeRoot's /K is a single ref to a /Document element (bare
      "/K 47 0 R" OR bracketed "/K [ 47 0 R ]" — our scaffold uses the
      bracketed form), return that element's xref.
    - If StructTreeRoot's /K is already a flat multi-entry array (no
      /Document wrapper), return the StructTreeRoot xref itself so we
      append directly to it.
    """
    struct_root_xref = _get_struct_root_xref(doc)
    struct_root = doc.xref_object(struct_root_xref)

    # Case 1: single /K ref, with or without surrounding brackets ->
    # check if it points at a /Document element
    single = re.search(r'/K\s*\[?\s*(\d+)\s+0\s+R\s*\]?', struct_root)
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
    Surgically locates and wraps ONLY the target image drawing sequence in a /Figure <</MCID n>> BDC ... EMC pair.
    Never consumes preceding or trailing text content streams.
    """
    print(f"🦜 [Polly Core] rewrite_content_stream: page={page_num} imgIdx={img_index} mcid={mcid}")
    page = doc[page_num]
    content = page.read_contents().decode("latin-1")

    clean_name = img_name.strip()
    if clean_name and not clean_name.startswith("/"):
        clean_name = "/" + clean_name

    do_pos = -1
    if clean_name:
        name_match = re.search(re.escape(clean_name) + r'\s+Do\b', content)
        if name_match:
            do_pos = name_match.start()

    if do_pos == -1:
        do_matches = list(re.finditer(r'/\w+\s+Do', content))
        if img_index < len(do_matches):
            do_pos = do_matches[img_index].start()
        else:
            do_matches_fallback = list(re.finditer(r'/Im\d+\s+Do', content))
            if img_index < len(do_matches_fallback):
                do_pos = do_matches_fallback[img_index].start()

    if do_pos == -1:
        raise ValueError(f"Could not locate image target draw token on page {page_num+1}")

    pre = content[:do_pos]
    post = content[do_pos:]

    # Find the immediate q before do_pos
    q_pos = -1
    for qm in re.finditer(r'(?:^|[\n ])q(?:[\n ]|$)', pre):
        q_pos = qm.start()

    # Find the matching Q after do_pos
    q_close = re.search(r'(?:^|[\n ])Q(?:[\n ]|$)', post)

    if q_pos >= 0 and q_close:
        close_pos = do_pos + q_close.end()
        block_content = content[q_pos:close_pos]
        # Verify block strictly contains ONLY this single image draw sequence
        if block_content.count(" Do") == 1 and "BT" not in block_content:
            q_char_pos = q_pos + 1 if content[q_pos] in (' ', '\n') else q_pos
            img_block = content[q_char_pos:close_pos]
            new_block = f"\n/Figure << /MCID {mcid} >>BDC\n{img_block.strip()}\nEMC\n"
            new_content = content[:q_char_pos] + new_block + content[close_pos:]
            contents_xref = _get_contents_xref(doc, page_num)
            doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
            return True

    # Fallback: wrap just the Do line itself
    end_line_match = re.search(r'[\n]|Q', post)
    end_line_pos = do_pos + (end_line_match.end() if end_line_match else len(post))
    target_draw_cmd = content[do_pos:end_line_pos].strip()
    new_content = (
        content[:do_pos]
        + f"\n/Figure << /MCID {mcid} >>BDC\n{target_draw_cmd}\nEMC\n"
        + content[end_line_pos:]
    )
    contents_xref = _get_contents_xref(doc, page_num)
    doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
    return True


def rewrite_content_stream_for_text_blocks(
    doc: fitz.Document, page_num: int, first_mcid: int
) -> list[int]:
    """
    Wraps every top-level BT...ET text object in the page's content stream
    in its own /P << /MCID n >> BDC ... EMC pair, in the order the blocks
    appear in the stream. Assigns sequential MCIDs starting at first_mcid.

    NOTE: this assumes BT...ET blocks appear in the content stream in the
    same order get_text_blocks_in_order() reports visually — true for the
    single-column documents we've tested so far. Multi-column layouts may
    need smarter matching later (tracked as a follow-up).
    """
    page = doc[page_num]
    content = page.read_contents().decode("latin-1")

    bt_et_matches = list(re.finditer(r'BT.*?ET', content, re.DOTALL))
    print(
        f"🦜 [Polly Core] rewrite_content_stream_for_text_blocks: page={page_num} "
        f"found {len(bt_et_matches)} BT..ET block(s), starting mcid={first_mcid}"
    )

    if not bt_et_matches:
        return []

    assigned_mcids = []
    new_content = ""
    cursor = 0
    for i, m in enumerate(bt_et_matches):
        mcid = first_mcid + i
        assigned_mcids.append(mcid)
        new_content += content[cursor:m.start()]
        new_content += f"/P << /MCID {mcid} >>BDC\n{m.group(0)}\nEMC\n"
        cursor = m.end()
    new_content += content[cursor:]

    contents_xref = _get_contents_xref(doc, page_num)
    print(f"🦜 [Polly Core] text block content rewrite: new length={len(new_content)}")
    doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)

    return assigned_mcids


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


def create_text_struct_element(
    doc: fitz.Document, page_xref: int, mcid: int, doc_struct_xref: int
) -> int:
    """
    Creates a minimal /P struct element for a tagged text block.
    Unlike Figure elements, /P elements don't need /Alt or a /Layout bbox
    attribute — the actual text content lives in the content stream itself,
    the struct element just needs to point back at the right MCID and page.
    """
    p_xref = doc.get_new_xref()
    struct_obj = (
        f"<</S /P "
        f"/K {mcid} "
        f"/P {doc_struct_xref} 0 R "
        f"/Pg {page_xref} 0 R >>"
    )
    doc.update_object(p_xref, struct_obj)
    return p_xref


def append_to_document_struct_k(doc: fitz.Document, new_xref: int, page_xref: int = None) -> None:
    """
    Add the new struct element xref to the appropriate /K array.
    If page_xref is given, tries to insert after the last existing element
    for that same page (keeps same-page elements grouped); otherwise (or if
    no match is found) appends at the end.
    """
    target_xref = _get_document_struct_xref(doc)
    target_obj = doc.xref_object(target_xref)

    # Parse the existing K array entries
    k_match = re.search(r'/K\s*\[([^\]]+)\]', target_obj, re.DOTALL)
    if not k_match:
        raise ValueError("Could not find /K array in struct element")

    # IMPORTANT: capture the FULL reference ("73 0 R"), not just the digits.
    # A previous version captured only the number, which silently turned
    # every existing entry into a bare, meaningless integer the first time
    # this function ran — corrupting the entire Document element's /K
    # array and orphaning every pre-existing tagged element in the doc.
    entries = re.findall(r'\d+\s+0\s+R', k_match.group(1))
    new_ref = f"{new_xref} 0 R"

    # Find the last entry whose /Pg matches our page_xref, if one was given
    insert_after = -1
    if page_xref is not None:
        for i, entry_ref in enumerate(entries):
            try:
                entry_xref = int(entry_ref.split()[0])
                obj = doc.xref_object(entry_xref)
                pg = re.search(r'/Pg\s+(\d+)\s+0\s+R', obj)
                if pg and int(pg.group(1)) == page_xref:
                    insert_after = i
            except Exception:
                pass

    if insert_after >= 0:
        # Insert after the last same-page element
        entries.insert(insert_after + 1, new_ref)
    else:
        # No page_xref given, or no existing elements on this page found —
        # append at end
        entries.append(new_ref)

    # Rebuild the K array
    entries_str = '\n      '.join(entries)
    updated = re.sub(
        r'(/K\s*\[)[^\]]+(\])',
        lambda m: m.group(1) + '\n      ' + entries_str + ' ' + m.group(2),
        target_obj,
        flags=re.DOTALL
    )

    if updated == target_obj:
        raise ValueError("Could not update /K array in struct element")

    doc.update_object(target_xref, updated)


def update_parent_tree(doc: fitz.Document, mcid: int, struct_xref: int, page_num: int = 0) -> None:
    """
    Add a new MCID->struct_xref mapping to the ParentTree.

    The ParentTree /Nums array maps PAGE SLOT indices (the /StructParents
    value on each page dict) to arrays of struct element refs. This is NOT
    the same as MCID numbers — using page_num's actual /StructParents value
    to find the correct slot is what avoids corrupting multi-page documents
    (an earlier version hardcoded slot 0, which broke pages 2+).
    """
    pt_xref = _get_parent_tree_xref(doc)
    pt_obj = doc.xref_object(pt_xref)

    # Get the page's StructParents slot number
    page_obj = doc.xref_object(doc[page_num].xref)
    sp_m = re.search(r'/StructParents\s+(\d+)', page_obj)
    page_slot = int(sp_m.group(1)) if sp_m else 0

    print(f"🦜 [Polly Core] update_parent_tree: page={page_num} slot={page_slot} mcid={mcid} struct_xref={struct_xref}")

    # Look for existing array for this page's slot
    slot_array_m = re.search(
        rf'({page_slot}\s+\[)([^\]]*?)(\])',
        pt_obj
    )
    if slot_array_m:
        existing_entries = slot_array_m.group(2)
        # Handle empty array — can't just append with leading newline
        if existing_entries.strip():
            new_entries = existing_entries.rstrip() + f"\n        {struct_xref} 0 R "
        else:
            new_entries = f" {struct_xref} 0 R "
        updated = (
            pt_obj[:slot_array_m.start(2)]
            + new_entries
            + pt_obj[slot_array_m.end(2):]
        )
        doc.update_object(pt_xref, updated)
        return

    # No existing array for this slot — append new Nums entry
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


def _uses_page_local_mcids(doc: fitz.Document, page_num: int) -> bool:
    """
    Detect whether this PDF uses page-local MCID arrays (Acrobat style)
    vs global MCID keys (InDesign style).
    Page-local: ParentTree[StructParents] -> an array (either an INLINE
    array literal, e.g. "0 [ null null 98 0 R ]", or an indirect reference
    to a separate array object — real-world Acrobat/LibreOffice PDFs use
    the inline form).
    Global: ParentTree[StructParents] -> a single struct element ref.
    """
    try:
        page = doc[page_num]
        page_obj = doc.xref_object(page.xref)
        sp_match = re.search(r'/StructParents\s+(\d+)', page_obj)
        if not sp_match:
            return False
        sp_val = int(sp_match.group(1))

        pt_xref = _get_parent_tree_xref(doc)
        pt = doc.xref_object(pt_xref)

        # Inline array form: "<sp_val> [ ... ]" directly in the ParentTree
        if re.search(rf'\b{sp_val}\s+\[', pt):
            return True

        # Indirect-reference form: "<sp_val> <xref> 0 R" pointing at a
        # separate object — check whether that object is an array or a dict
        entry_match = re.search(rf'\b{sp_val}\s+(\d+)\s+0\s+R', pt)
        if not entry_match:
            return False

        candidate_xref = int(entry_match.group(1))
        candidate = doc.xref_object(candidate_xref)
        return candidate.strip().startswith('[')
    except Exception:
        return False


def _get_page_local_slot_entries(doc: fitz.Document, page_num: int) -> tuple[list, bool, int]:
    """
    Returns (entries, is_inline, source_xref) for this page's page-local
    MCID slot array, where entries is a list of tokens ('null' or 'N 0 R')
    in index order (index == MCID).

    is_inline=True  -> the array lives directly inside the ParentTree object
                        itself (source_xref is the ParentTree's own xref)
    is_inline=False -> the array is a separate indirect object
                        (source_xref is that array object's xref)
    """
    page = doc[page_num]
    page_obj = doc.xref_object(page.xref)
    sp_val = int(re.search(r'/StructParents\s+(\d+)', page_obj).group(1))

    pt_xref = _get_parent_tree_xref(doc)
    pt = doc.xref_object(pt_xref)

    inline_m = re.search(rf'\b{sp_val}\s+\[([^\]]*)\]', pt)
    if inline_m:
        entries = re.findall(r'(\d+\s+0\s+R|null)', inline_m.group(1))
        return entries, True, pt_xref

    ref_m = re.search(rf'\b{sp_val}\s+(\d+)\s+0\s+R', pt)
    if ref_m:
        arr_xref = int(ref_m.group(1))
        arr_obj = doc.xref_object(arr_xref)
        entries = re.findall(r'(\d+\s+0\s+R|null)', arr_obj)
        return entries, False, arr_xref

    raise ValueError(f"No page-local ParentTree slot found for StructParents {sp_val}")


def _get_next_free_mcid_in_page_array(doc: fitz.Document, page_num: int) -> int:
    """
    Find the first null slot index in a page's StructParents array.
    That index becomes the MCID for our new Figure.
    """
    entries, _, _ = _get_page_local_slot_entries(doc, page_num)
    for i, slot in enumerate(entries):
        if slot == 'null':
            return i
    return len(entries)  # no nulls — append as a new index


def _insert_struct_elem_into_page_array(
    doc: fitz.Document, page_num: int, slot_index: int, fig_xref: int
) -> None:
    """
    Replace the null at slot_index in this page's StructParents array with
    our new struct element xref — handling both the inline-array case
    (splice the rebuilt array back into the ParentTree object itself) and
    the indirect-array case (update that separate array object directly).
    """
    entries, is_inline, source_xref = _get_page_local_slot_entries(doc, page_num)

    if slot_index < len(entries) and entries[slot_index] == 'null':
        entries[slot_index] = f'{fig_xref} 0 R'
    else:
        entries.append(f'{fig_xref} 0 R')

    new_entries_str = ' '.join(entries)

    if is_inline:
        pt_obj = doc.xref_object(source_xref)
        page = doc[page_num]
        page_obj = doc.xref_object(page.xref)
        sp_val = int(re.search(r'/StructParents\s+(\d+)', page_obj).group(1))
        updated = re.sub(
            rf'(\b{sp_val}\s+\[)[^\]]*(\])',
            lambda m: m.group(1) + new_entries_str + ' ' + m.group(2),
            pt_obj,
            count=1,
        )
        if updated == pt_obj:
            raise ValueError(f"Could not update inline slot array for StructParents {sp_val}")
        doc.update_object(source_xref, updated)
    else:
        new_arr = '[ ' + new_entries_str + ' ]'
        doc.update_object(source_xref, new_arr)


def _find_existing_figure_for_image(doc: fitz.Document, page_num: int, img_index: int) -> int:
    """
    Look for a Figure struct element that ALREADY covers this image, so we
    can just update its /Alt instead of creating a redundant, nested
    duplicate. Real-world tagged PDFs often use a generic BDC operator name
    (e.g. "/P << /MCID 16 >> BDC") for what the struct tree still reports
    as /S /Figure — the operator name in the content stream is NOT reliable
    evidence of the structural role, so we match purely by MCID number and
    by the existing struct element's own /S /Figure + /Pg fields, not by
    requiring the literal text "/Figure" to appear in the stream.

    Returns the existing Figure's xref, or -1 if none is found.
    """
    page_xref = doc[page_num].xref
    content = doc[page_num].read_contents().decode("latin-1", errors="ignore")

    # Any BDC-tagged MCID in stream order, regardless of operator name
    all_bdc_mcids = [int(m) for m in re.findall(r'<<\s*/MCID\s+(\d+)\s*>>\s*BDC', content)]
    print(f"🦜 [Polly Core] _find_existing_figure_for_image: page={page_num} imgIdx={img_index} all_bdc_mcids={all_bdc_mcids}")

    target_mcid = all_bdc_mcids[img_index] if img_index < len(all_bdc_mcids) else None

    # Collect existing Figure struct elements on this page, in xref order
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
            k_m = re.search(r'/K\s+\[?\s*(\d+)\s*\]?', obj)
            page_figures.append((xref, int(k_m.group(1)) if k_m else -1))
        except Exception:
            pass

    if target_mcid is not None:
        for xref, mcid in page_figures:
            if mcid == target_mcid:
                return xref

    # Fallback: nth Figure element by xref order (Word/Quartz-style PDFs
    # with struct trees but no BDC markers at all for this image)
    if img_index < len(page_figures):
        return page_figures[img_index][0]

    return -1

def _find_do_position(content: str, img_index: int, img_name: str) -> int:
    """
    Shared "find where this image is drawn" logic, factored out so both the
    tight-wrap logic and the enclosing-region-detection logic use identical
    matching rules and always agree on which Do call we mean.
    """
    clean_name = img_name.strip()
    if clean_name and not clean_name.startswith("/"):
        clean_name = "/" + clean_name

    do_pos = -1
    if clean_name:
        name_match = re.search(re.escape(clean_name) + r'\s+Do\b', content)
        if name_match:
            do_pos = name_match.start()

    if do_pos == -1:
        do_matches = list(re.finditer(r'/\w+\s+Do', content))
        if img_index < len(do_matches):
            do_pos = do_matches[img_index].start()
        else:
            do_matches_fallback = list(re.finditer(r'/Im\d+\s+Do', content))
            if img_index < len(do_matches_fallback):
                do_pos = do_matches_fallback[img_index].start()

    return do_pos


def _find_enclosing_open_bdc(content: str, do_pos: int) -> dict:
    """
    Returns info about the innermost marked-content region (BDC OR BMC —
    both need tracking to keep nesting depth balanced, even though only
    BDC regions with an /MCID are candidates to return) that is still open
    at do_pos. Returns None if do_pos isn't inside any such region, or if
    the only open region has no /MCID (e.g. a plain /Artifact BMC).
    """
    open_re = re.compile(r'/(\w+)\s*(?:<<([^>]*)>>)?\s*(BDC|BMC)')
    close_re = re.compile(r'\bEMC\b')

    tokens = []
    for m in open_re.finditer(content):
        if m.start() > do_pos:
            break
        mcid_m = re.search(r'/MCID\s+(\d+)', m.group(2) or '')
        tokens.append((m.start(), 'open', m.group(1), int(mcid_m.group(1)) if mcid_m else None, m.end()))
    for m in close_re.finditer(content):
        if m.start() > do_pos:
            break
        tokens.append((m.start(), 'close', None, None, m.end()))
    tokens.sort(key=lambda t: t[0])

    stack = []
    for pos, kind, tag, mcid, end in tokens:
        if kind == 'open':
            stack.append({'tag': tag, 'mcid': mcid, 'open_start': pos, 'open_end': end})
        elif stack:
            stack.pop()

    for entry in reversed(stack):
        if entry['mcid'] is not None:
            return entry
    return None


def _find_matching_emc(content: str, search_start: int) -> tuple:
    """
    Given a position right after an already-open BDC/BMC (depth 1), scans
    forward to find (start, end) of the EMC that closes it, correctly
    accounting for further BDC or BMC nesting in between (e.g. a nested
    /Artifact BMC...EMC pair for a different image sitting inside the same
    outer region).
    """
    depth = 1
    for m in re.finditer(r'\bBDC\b|\bBMC\b|\bEMC\b', content[search_start:]):
        if m.group(0) in ('BDC', 'BMC'):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return (search_start + m.start(), search_start + m.end())
    return (len(content), len(content))


def analyze_figure_insertion(doc: fitz.Document, page_num: int, img_index: int, img_name: str) -> dict:
    """
    Figures out WHERE and HOW to insert a new Figure BDC/EMC for this image,
    and — critically — whether doing so tightly would end up nested inside
    a pre-existing marked-content region that covers more than just this
    image (e.g. a /P that also wraps a neighboring artifact image and a
    decorative border stroke). If so, that outer region needs to be SPLIT
    around our new Figure so the Figure becomes a sibling rather than a
    buried child — nesting like that is what causes some readers to defer
    announcing the Figure's alt text until the whole outer region closes.
    """
    page = doc[page_num]
    content = page.read_contents().decode("latin-1")

    do_pos = _find_do_position(content, img_index, img_name)
    if do_pos == -1:
        raise ValueError(f"Could not locate image target draw token on page {page_num+1}")

    pre = content[:do_pos]
    post = content[do_pos:]

    q_pos = -1
    for qm in re.finditer(r'(?:^|[\n ])q(?:[\n ]|$)', pre):
        q_pos = qm.start()
    q_close = re.search(r'(?:^|[\n ])Q(?:[\n ]|$)', post)

    if q_pos < 0 or not q_close:
        raise ValueError(f"Could not locate a clean q...Q wrap around the image on page {page_num+1}")

    close_pos = do_pos + q_close.end()
    q_char_pos = q_pos + 1 if content[q_pos] in (' ', '\n') else q_pos

    needs_split = False
    outer_tag = None
    outer_mcid = None

    enclosing = _find_enclosing_open_bdc(content, do_pos)
    if enclosing is not None:
        emc_start, _ = _find_matching_emc(content, enclosing['open_end'])
        outer_span = content[enclosing['open_end']:emc_start]
        # "Tight" = the outer region is basically just our image (one Do,
        # no text). More than that (another image, a stroke, etc.) means
        # this region has nothing structurally to do with just our image,
        # so we split rather than bury our Figure inside it.
        if outer_span.count(" Do") > 1 or "BT" in outer_span:
            needs_split = True
            outer_tag = enclosing['tag']
            outer_mcid = enclosing['mcid']

    print(
        f"🦜 [Polly Core] analyze_figure_insertion: page={page_num} imgIdx={img_index} "
        f"needs_split={needs_split} outer_tag={outer_tag} outer_mcid={outer_mcid}"
    )

    return {
        'content': content,
        'q_char_pos': q_char_pos,
        'close_pos': close_pos,
        'needs_split': needs_split,
        'outer_tag': outer_tag,
        'outer_mcid': outer_mcid,
    }


def rewrite_content_stream_for_new_figure(
    doc: fitz.Document, page_num: int, analysis: dict, fig_mcid: int, continuation_mcid: int = None
) -> bool:
    """
    Performs the actual content-stream edit described by analyze_figure_insertion().
    If needs_split is False: tight-wraps just the image in a new Figure BDC/EMC.
    If needs_split is True: closes the pre-existing outer region early, inserts
    the tightly-wrapped Figure as a sibling, then reopens the outer region
    (same tag, new continuation_mcid) so its remaining content — and its own
    original closing EMC, untouched — keeps working exactly as before.
    """
    content = analysis['content']
    q_char_pos = analysis['q_char_pos']
    close_pos = analysis['close_pos']
    img_block = content[q_char_pos:close_pos].strip()

    if analysis['needs_split']:
        outer_tag = analysis['outer_tag']
        print(
            f"🦜 [Polly Core] Splitting enclosing /{outer_tag} MCID {analysis['outer_mcid']} "
            f"around new Figure MCID {fig_mcid}; continuation MCID {continuation_mcid}"
        )
        new_content = (
            content[:q_char_pos]
            + "EMC\n"
            + f"/Figure << /MCID {fig_mcid} >>BDC\n{img_block}\nEMC\n"
            + f"/{outer_tag} << /MCID {continuation_mcid} >>BDC\n"
            + content[close_pos:]
        )
    else:
        new_content = (
            content[:q_char_pos]
            + f"/Figure << /MCID {fig_mcid} >>BDC\n{img_block}\nEMC\n"
            + content[close_pos:]
        )

    contents_xref = _get_contents_xref(doc, page_num)
    doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
    return True


def _extend_struct_elem_k(doc: fitz.Document, struct_xref: int, extra_mcid: int) -> None:
    """
    Extend a struct element's /K from a bare integer (a single MCID) into
    an array covering both the original MCID and a new one. Used when we
    split a pre-existing marked-content region to make room for a newly
    inserted sibling Figure in the middle of it — the original struct
    element's content is now split across two ranges on the same page,
    which /K as an array of integers is explicitly designed to represent.
    """
    obj = doc.xref_object(struct_xref)
    k_arr_m = re.search(r'/K\s*\[([^\]]*)\]', obj)
    if k_arr_m:
        updated = re.sub(
            r'(/K\s*\[[^\]]*)\]',
            lambda m: m.group(1) + f' {extra_mcid}]',
            obj,
        )
    else:
        k_int_m = re.search(r'/K\s+(\d+)\b', obj)
        if not k_int_m:
            raise ValueError(f"Could not find /K in struct element xref={struct_xref}")
        orig_mcid = k_int_m.group(1)
        updated = re.sub(r'/K\s+\d+\b', f'/K [ {orig_mcid} {extra_mcid} ]', obj)
    doc.update_object(struct_xref, updated)

def _create_continuation_struct_element(
    doc: fitz.Document, page_xref: int, mcid: int, doc_struct_xref: int, source_xref: int
) -> int:
    """
    Creates a brand-new struct element that continues a split marked-content
    region as a true SIBLING of the original — not merged into it. This
    matters: a single element with a multi-entry /K (e.g. [12, 14]) gets
    read as one atomic, uninterrupted unit by some readers regardless of
    what's been inserted into the content stream between those MCIDs. Two
    separate elements, correctly ordered in Document's /K, actually get
    interrupted by whatever sits between them in that array — which is
    what we need for our new Figure to be heard in the right spot.

    Copies the original element's /S role (e.g. /Figure) but deliberately
    does NOT copy /Alt — the alt text belongs on our new Figure, not on
    this leftover continuation of the pre-existing container.
    """
    source_obj = doc.xref_object(source_xref)
    s_m = re.search(r'/S\s+(/\w+)', source_obj)
    role = s_m.group(1) if s_m else '/Figure'

    cont_xref = doc.get_new_xref()
    struct_obj = (
        f"<</Type /StructElem "
        f"/S {role} "
        f"/K {mcid} "
        f"/P {doc_struct_xref} 0 R "
        f"/Pg {page_xref} 0 R >>"
    )
    doc.update_object(cont_xref, struct_obj)
    return cont_xref


def _insert_siblings_after(doc: fitz.Document, after_xref: int, new_xrefs: list) -> None:
    """
    Insert new_xrefs into Document's /K array immediately after a SPECIFIC
    existing element's own entry — not "at the end of this page's block"
    like append_to_document_struct_k does. Needed for split-region
    insertion, where reading order must literally interleave: [original
    element] -> [our new Figure] -> [continuation element] -> whatever
    came after the original before.
    """
    target_xref = _get_document_struct_xref(doc)
    target_obj = doc.xref_object(target_xref)

    k_match = re.search(r'/K\s*\[([^\]]+)\]', target_obj, re.DOTALL)
    if not k_match:
        raise ValueError("Could not find /K array in struct element")

    entries = re.findall(r'\d+\s+0\s+R', k_match.group(1))
    after_ref = f"{after_xref} 0 R"
    try:
        idx = entries.index(after_ref)
    except ValueError:
        raise ValueError(f"Could not find xref {after_xref} in Document /K to insert after")

    for offset, nx in enumerate(new_xrefs):
        entries.insert(idx + 1 + offset, f"{nx} 0 R")

    entries_str = '\n      '.join(entries)
    updated = re.sub(
        r'(/K\s*\[)[^\]]+(\])',
        lambda m: m.group(1) + '\n      ' + entries_str + ' ' + m.group(2),
        target_obj,
        flags=re.DOTALL
    )
    doc.update_object(target_xref, updated)

def inject_alt_tagged(doc: fitz.Document, page_num: int, img_index: int, alt_text: str, img_name: str = "") -> str:
    """
    Full tagged-PDF remediation pipeline for one image.
    Fast path: if a Figure struct element already covers this image, just
    update its /Alt — avoids creating a redundant, nested duplicate Figure
    around the same content.
    Full path: rewrite content stream + wire a brand-new struct element,
    handling both page-local (Acrobat) and global (InDesign) MCID systems.
    """
    safe_alt = alt_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    existing_xref = _find_existing_figure_for_image(doc, page_num, img_index)
    if existing_xref != -1:
        obj = doc.xref_object(existing_xref)
        if "/Alt" in obj:
            updated = re.sub(r"/Alt\s*\([^)]*\)", f"/Alt ({safe_alt}\\000)", obj)
        else:
            updated = obj.rstrip().rstrip(">").rstrip() + f"\n  /Alt ({safe_alt}\\000)\n>>"
        doc.update_object(existing_xref, updated)
        print(f"🦜 [Polly Core] Fast path: updated existing Figure xref={existing_xref}")
        return (
            f"Tagged (fast path): page {page_num+1}, imgIdx {img_index} "
            f"-> existing Figure xref {existing_xref}"
        )

    page_xref = doc[page_num].xref

    if _uses_page_local_mcids(doc, page_num):
        # PAGE-LOCAL path: MCID = slot index in the page's StructParents array
        analysis = analyze_figure_insertion(doc, page_num, img_index, img_name)
        fig_mcid = _get_next_free_mcid_in_page_array(doc, page_num)

        continuation_mcid = None
        if analysis['needs_split']:
            # Reserve fig_mcid's slot mentally, then find the NEXT free
            # null slot for the outer region's continuation — can't just
            # call _get_next_free_mcid_in_page_array() again since fig_mcid
            # hasn't actually been written yet at this point.
            entries, _, _ = _get_page_local_slot_entries(doc, page_num)
            probe = entries.copy()
            if fig_mcid < len(probe):
                probe[fig_mcid] = 'reserved'
            else:
                probe.append('reserved')
            continuation_mcid = next((i for i, s in enumerate(probe) if s == 'null'), len(probe))

        rewrite_content_stream_for_new_figure(doc, page_num, analysis, fig_mcid, continuation_mcid)
        fig_xref = create_figure_struct_element(doc, page_xref, fig_mcid, alt_text, page_num, img_index)
        _insert_struct_elem_into_page_array(doc, page_num, fig_mcid, fig_xref)

        split_note = ""
        if analysis['needs_split']:
            # Find the ORIGINAL element being split (still holds only its
            # first MCID at this point — we haven't touched it)
            outer_entries, _, _ = _get_page_local_slot_entries(doc, page_num)
            outer_xref = int(outer_entries[analysis['outer_mcid']].split()[0])

            doc_struct_xref = _get_document_struct_xref(doc)
            cont_xref = _create_continuation_struct_element(
                doc, page_xref, continuation_mcid, doc_struct_xref, outer_xref
            )

            # Insert BOTH new elements right after the original's own
            # position in Document /K — NOT at the end of the page block —
            # so reading order truly interleaves: original -> our Figure ->
            # continuation -> rest of page.
            _insert_siblings_after(doc, outer_xref, [fig_xref, cont_xref])
            _insert_struct_elem_into_page_array(doc, page_num, continuation_mcid, cont_xref)

            split_note = (
                f", split enclosing /{analysis['outer_tag']} MCID "
                f"{analysis['outer_mcid']}+{continuation_mcid} "
                f"(original xref {outer_xref}, continuation xref {cont_xref})"
            )
        else:
            append_to_document_struct_k(doc, fig_xref, page_xref)
        # No ParentTreeNextKey to update in page-local mode

        return (
            f"Tagged (page-local): page {page_num+1}, imgIdx {img_index} "
            f"-> MCID {fig_mcid} (slot in StructParents array), struct xref {fig_xref}"
            + split_note
        )
    else:
        # GLOBAL path: MCID = ParentTreeNextKey, added as new top-level Nums entry
        analysis = analyze_figure_insertion(doc, page_num, img_index, img_name)
        fig_mcid = get_struct_tree_next_key(doc)
        continuation_mcid = fig_mcid + 1 if analysis['needs_split'] else None

        rewrite_content_stream_for_new_figure(doc, page_num, analysis, fig_mcid, continuation_mcid)
        fig_xref = create_figure_struct_element(doc, page_xref, fig_mcid, alt_text, page_num, img_index)
        update_parent_tree(doc, fig_mcid, fig_xref)

        max_mcid_used = fig_mcid
        split_note = ""
        if analysis['needs_split']:
            pt_xref = _get_parent_tree_xref(doc)
            pt_obj = doc.xref_object(pt_xref)
            outer_ref_m = re.search(rf'\b{analysis["outer_mcid"]}\s+(\d+)\s+0\s+R', pt_obj)
            if outer_ref_m:
                outer_xref = int(outer_ref_m.group(1))
                doc_struct_xref = _get_document_struct_xref(doc)
                cont_xref = _create_continuation_struct_element(
                    doc, page_xref, continuation_mcid, doc_struct_xref, outer_xref
                )
                _insert_siblings_after(doc, outer_xref, [fig_xref, cont_xref])
                update_parent_tree(doc, continuation_mcid, cont_xref)
                split_note = (
                    f", split enclosing /{analysis['outer_tag']} MCID "
                    f"{analysis['outer_mcid']}+{continuation_mcid} "
                    f"(original xref {outer_xref}, continuation xref {cont_xref})"
                )
                max_mcid_used = continuation_mcid
            else:
                append_to_document_struct_k(doc, fig_xref)
        else:
            append_to_document_struct_k(doc, fig_xref)

        increment_parent_tree_next_key(doc, max_mcid_used)

        return (
            f"Tagged (global): page {page_num+1}, imgIdx {img_index} "
            f"-> MCID {fig_mcid}, struct xref {fig_xref}"
            + split_note
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

def get_text_blocks_in_order(doc: fitz.Document, page_num: int) -> list[dict]:
    """
    Extract all text LINES on a page via PyMuPDF's text dict and return them
    in a naive single-column reading order (top-to-bottom, then left-to-right
    for lines that land in roughly the same row).

    IMPORTANT: this operates at LINE granularity, not paragraph/block
    granularity, because PDF content streams typically open a new BT...ET
    text object per line rather than per paragraph. Matching granularity
    1:1 with rewrite_content_stream_for_text_blocks() is what lets each
    wrapped MCID map back to a real struct element instead of being orphaned.

    This does NOT touch the content stream — it's a read-only inventory step.

    Column detection is NOT handled here yet — multi-column layouts will sort
    incorrectly until that's added as a follow-up.
    """
    page = doc[page_num]
    raw = page.get_text("dict")

    lines = []
    for b in raw.get("blocks", []):
        if b.get("type") != 0:
            continue  # type 1 = image block; images are handled by the existing Figure pipeline

        for line in b.get("lines", []):
            text = "".join(span["text"] for span in line.get("spans", []))
            if not text.strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            lines.append({"bbox": (x0, y0, x1, y1), "text": text})

    # Naive reading order: round y0 into "rows" so lines that are visually
    # side-by-side don't get separated purely by sub-pixel y differences,
    # then sort by x0 within a row. ROW_TOLERANCE may need tuning per-document.
    ROW_TOLERANCE = 3.0
    lines.sort(key=lambda ln: (round(ln["bbox"][1] / ROW_TOLERANCE), ln["bbox"][0]))

    for i, ln in enumerate(lines):
        ln["order_index"] = i

    print(f"🦜 [Polly Core] page {page_num+1}: found {len(lines)} text line(s)")
    for ln in lines:
        preview = ln["text"].strip().replace("\n", " ")[:60]
        print(
            f"🦜 [Polly Core]   [{ln['order_index']}] "
            f"y0={ln['bbox'][1]:.1f} x0={ln['bbox'][0]:.1f} — {preview!r}"
        )

    return lines

def _ensure_struct_tree(doc: fitz.Document) -> None:
    cat_xref = doc.pdf_catalog()
    cat_obj = doc.xref_object(cat_xref)

    if "/StructTreeRoot" in cat_obj:
        return

    print("🦜 [Polly Core] Building struct tree scaffold for untagged PDF")

    # 1. ParentTree — pre-populate one empty slot per page. This is required:
    # update_parent_tree() below finds the right slot by regex-matching
    # "<slot> [...]", which only works if every page's slot already exists
    # as its own empty array rather than one shared empty /Nums [].
    pt_xref = doc.get_new_xref()
    print(f"🦜 [Polly Core] scaffold step 1: ParentTree xref={pt_xref}")
    nums_entries = ""
    for i in range(doc.page_count):
        nums_entries += f"\n    {i} []"
    doc.update_object(pt_xref, f"<< /Nums [{nums_entries}\n  ] >>")

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
    a /Figure element with /Alt for the target image AND /P elements for
    every text block on the page, wired into the struct tree in visual
    top-to-bottom reading order.
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

    page_xref = page.xref
    doc_struct_xref = _get_document_struct_xref(doc)

    # Get the image's bbox so we can sort it against text blocks by position
    try:
        img_infos = page.get_image_info(hashes=False)
        raster_infos = [i for i in img_infos if i.get('width', 0) > 1]
        img_bbox = raster_infos[img_index]['bbox'] if img_index < len(raster_infos) else (0, 0, 612, 792)
    except Exception:
        img_bbox = (0, 0, 612, 792)
    img_y0 = img_bbox[1]

    # Inventory the text blocks on this page in naive reading order
    text_blocks = get_text_blocks_in_order(doc, page_num)

    # Assign an MCID + wrap the image draw command in a Figure BDC/EMC
    img_mcid = get_struct_tree_next_key(doc)
    rewrite_content_stream_by_name_or_index(
        doc, page_num, img_index,
        _get_image_resource_name(doc, page_num, img_xref),
        img_mcid
    )
    fig_xref = create_figure_struct_element(
        doc, page_xref, img_mcid, alt_text, page_num, img_index
    )

    # Assign MCIDs + wrap every BT...ET text object on this page.
    # IMPORTANT: start right after img_mcid rather than re-querying
    # get_struct_tree_next_key() again — that key isn't bumped until
    # increment_parent_tree_next_key() runs at the end of this function,
    # so a second call here would just return the same stale value and
    # collide with img_mcid.
    text_start_mcid = img_mcid + 1
    text_mcids = rewrite_content_stream_for_text_blocks(doc, page_num, text_start_mcid)

    if len(text_mcids) != len(text_blocks):
        print(
            f"🦜 [Polly Core] ⚠️ page {page_num+1}: line count ({len(text_blocks)}) "
            f"!= BT..ET count ({len(text_mcids)}) — some tagged regions won't "
            f"get a matching struct element"
        )

    text_xrefs = [
        create_text_struct_element(doc, page_xref, mcid, doc_struct_xref)
        for mcid in text_mcids
    ]

    print(f"🦜 [Polly Core] page {page_num+1}: img_y0={img_y0:.1f}")

    # Combine image + text into one reading-order sequence, sorted by y0
    items = [{"y0": img_y0, "mcid": img_mcid, "xref": fig_xref}]
    for blk, mcid, xref in zip(text_blocks, text_mcids, text_xrefs):
        items.append({"y0": blk["bbox"][1], "mcid": mcid, "xref": xref})
    items.sort(key=lambda it: it["y0"])

    print(f"🦜 [Polly Core] page {page_num+1}: wiring {len(items)} struct element(s) in reading order:")
    for it in items:
        print(f"🦜 [Polly Core]   y0={it['y0']:.1f} mcid={it['mcid']} xref={it['xref']}")

    # Wire each struct element into the tree in that sorted order
    for it in items:
        append_to_document_struct_k(doc, it["xref"])
        update_parent_tree(doc, it["mcid"], it["xref"], page_num)

    increment_parent_tree_next_key(doc, max(it["mcid"] for it in items))

    return (
        f"Untagged+scaffold: page {page_num+1}, imgIdx {img_index}, "
        f"MCID {img_mcid}, struct xref {fig_xref}, "
        f"+{len(text_mcids)} text block(s) tagged"
    )


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

def mark_image_as_artifact(doc: fitz.Document, page_num: int, img_index: int, img_name: str = "") -> str:
    """
    Converts an image into a Decorative Artifact:
    1. Strips any existing /Alt attributes from structure tree elements or image XObjects.
    2. Surgically wraps ONLY the image draw command in a valid /Artifact BMC ... EMC block.
    """
    page = doc[page_num]
    page_xref = page.xref

    # 1. Strip /Alt from any existing /Figure structure elements for this page
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
            if obj and '/Figure' in obj and f'/Pg {page_xref} 0 R' in obj:
                if '/Alt' in obj:
                    cleaned_obj = re.sub(r'/Alt\s*(\([^)]*\)|<[^>]*>)', '', obj)
                    doc.update_object(xref, cleaned_obj)
        except Exception:
            pass

    # 2. Strip /Alt from the image XObject dictionary itself if present
    try:
        images = page.get_images(full=True)
        raster = [i for i in images if i[2] > 1]
        if img_index < len(raster):
            img_xref = raster[img_index][0]
            img_obj = doc.xref_object(img_xref)
            if '/Alt' in img_obj:
                cleaned_img_obj = re.sub(r'/Alt\s*(\([^)]*\)|<[^>]*>)', '', img_obj)
                doc.update_object(img_xref, cleaned_img_obj)
    except Exception:
        pass

    # 3. Surgical Content Stream Rewrite: Wrap ONLY the image draw sequence in /Artifact BMC ... EMC
    content = page.read_contents().decode("latin-1")
    clean_name = img_name.strip()
    if clean_name and not clean_name.startswith("/"):
        clean_name = "/" + clean_name

    do_pos = -1
    if clean_name:
        m = re.search(re.escape(clean_name) + r'\s+Do\b', content)
        if m:
            do_pos = m.start()

    if do_pos == -1:
        do_matches = list(re.finditer(r'/\w+\s+Do', content))
        if img_index < len(do_matches):
            do_pos = do_matches[img_index].start()
        else:
            do_matches_fallback = list(re.finditer(r'/Im\d+\s+Do', content))
            if img_index < len(do_matches_fallback):
                do_pos = do_matches_fallback[img_index].start()

    if do_pos == -1:
        raise ValueError(f"Could not locate image target draw token on page {page_num+1}")

    pre = content[:do_pos]
    post = content[do_pos:]

    # Find immediate q before do_pos
    q_pos = -1
    for qm in re.finditer(r'(?:^|[\n ])q(?:[\n ]|$)', pre):
        q_pos = qm.start()

    # Find matching Q after do_pos
    q_close = re.search(r'(?:^|[\n ])Q(?:[\n ]|$)', post)

    if q_pos >= 0 and q_close:
        close_pos = do_pos + q_close.end()
        block_content = content[q_pos:close_pos]
        # Ensure block contains ONLY this single image draw sequence
        if block_content.count(" Do") == 1 and "BT" not in block_content:
            q_char_pos = q_pos + 1 if content[q_pos] in (' ', '\n') else q_pos
            img_block = content[q_char_pos:close_pos]
            new_block = f"\n/Artifact BMC\n{img_block.strip()}\nEMC\n"
            new_content = content[:q_char_pos] + new_block + content[close_pos:]
            contents_xref = _get_contents_xref(doc, page_num)
            doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)
            return f"Decorative Artifact: page {page_num+1}, imgIdx {img_index}"

    # Fallback: wrap just the Do line itself
    end_line_match = re.search(r'[\n]|Q', post)
    end_line_pos = do_pos + (end_line_match.end() if end_line_match else len(post))
    target_draw_cmd = content[do_pos:end_line_pos].strip()
    new_content = (
        content[:do_pos]
        + f"\n/Artifact BMC\n{target_draw_cmd}\nEMC\n"
        + content[end_line_pos:]
    )

    contents_xref = _get_contents_xref(doc, page_num)
    doc.update_stream(contents_xref, new_content.encode("latin-1"), compress=False)

    return f"Decorative Artifact: page {page_num+1}, imgIdx {img_index}"

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
                    status = mark_image_as_artifact(doc, page_num, img_index, img_name)
                    results.append(status)
                    print(f"🦜 [Polly Core] 🎨 {status}")
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

        # TEMP DIAGNOSTIC — dump the actual struct tree objects so we can see
        # what really landed before save() potentially rewrites/garbage-collects
        # anything. Remove once we've confirmed the tree is well-formed.
        try:
            print("🦜 [Polly Core] ===== STRUCT TREE DUMP =====")
            struct_root_xref = _get_struct_root_xref(doc)
            print(f"🦜 [Polly Core] StructTreeRoot (xref={struct_root_xref}):")
            print(doc.xref_object(struct_root_xref))

            doc_struct_xref = _get_document_struct_xref(doc)
            print(f"🦜 [Polly Core] Document elem (xref={doc_struct_xref}):")
            print(doc.xref_object(doc_struct_xref))

            pt_xref = _get_parent_tree_xref(doc)
            print(f"🦜 [Polly Core] ParentTree (xref={pt_xref}):")
            print(doc.xref_object(pt_xref))

            cat_xref = doc.pdf_catalog()
            print(f"🦜 [Polly Core] Catalog (xref={cat_xref}):")
            print(doc.xref_object(cat_xref))
            print("🦜 [Polly Core] ===== END STRUCT TREE DUMP =====")
        except Exception as e:
            print(f"🦜 [Polly Core] ⚠️ struct tree dump failed: {e}")

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
    enriched with matrix translation transformation orientations.
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