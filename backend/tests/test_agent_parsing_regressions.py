from io import BytesIO

from pypdf import PdfWriter

from app.agent.platform import _json_from_content as platform_json_from_content
from app.agent.services import _json_from_content as services_json_from_content
from app.settings.integrations import _extract_text_from_pdf_bytes


def test_json_parsers_accept_markdown_fenced_json():
    content = """```json
{"tipo_correo":"pedido","confianza":0.9}
```"""

    expected = {"tipo_correo": "pedido", "confianza": 0.9}

    assert platform_json_from_content(content) == expected
    assert services_json_from_content(content) == expected


def test_pdf_extractor_keeps_lightweight_fallback_for_empty_pdf():
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)

    assert _extract_text_from_pdf_bytes(buffer.getvalue()) == ""
