"""Write a small PDF with text at known positions, for the schematic_find tests. No PDF library is needed.

Every text is invented. Coordinates are PDF points with the origin at the bottom left of the page.
"""

from dataclasses import dataclass

LETTER_WIDTH_PT = 612
LETTER_HEIGHT_PT = 792
FONT_SIZE_PT = 12
HEADER = b"%PDF-1.4\n"


@dataclass(frozen=True)
class PdfText:
    x_pt: float
    y_pt: float
    text: str
    size_pt: float = FONT_SIZE_PT


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def content_stream(texts: list[PdfText]) -> bytes:
    lines = [f"BT /F1 {item.size_pt} Tf {item.x_pt} {item.y_pt} Td ({escape(item.text)}) Tj ET" for item in texts]
    return "\n".join(lines).encode("latin-1")


def make_pdf(pages: list[list[PdfText]], width_pt: int = LETTER_WIDTH_PT, height_pt: int = LETTER_HEIGHT_PT) -> bytes:
    """One page per list. Objects: 1 catalog, 2 page tree, 3 font, then a page and its content per page."""
    first_page = 4
    page_ids = [first_page + 2 * index for index in range(len(pages))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(pages)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, texts in zip(page_ids, pages, strict=True):
        stream = content_stream(texts)
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt} {height_pt}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
        ).encode()
        objects[page_id + 1] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
    output = bytearray(HEADER)
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(output)
        output += f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n"
    xref = len(output)
    count = max(objects) + 1
    output += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    for object_id in range(1, count):
        output += f"{offsets[object_id]:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)
