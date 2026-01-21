"""
Shared visual rendering logic for MCP tools.
"""
import io
import logging
from typing import List, Union
import asyncio

from pdf2image import convert_from_bytes, convert_from_path, pdfinfo_from_bytes, pdfinfo_from_path
from PIL import Image as PILImage
from fastmcp.utilities.types import Image


logger = logging.getLogger(__name__)

def _resize_if_needed(image: PILImage.Image, max_dim: int = None) -> PILImage.Image:
    """Helper to resize PIL image maintaining aspect ratio if it exceeds max_dim."""
    if not max_dim:
        return image
    
    w, h = image.size
    if max(w, h) <= max_dim:
        return image
        
    image.thumbnail((max_dim, max_dim), PILImage.Resampling.LANCZOS)
    return image

def render_image(
    image_input: Union[bytes, str],
    mime_type: str = "image/png",
    max_dimension: int = None
) -> list:
    """
    Renders an image from bytes or a file path.
    Returns a list with a single FastMCP Image object.
    
    Args:
        image_input: Image content as bytes or a file path string.
        mime_type: MIME type of the image (used if input is bytes).
        max_dimension: Optional maximum width or height.
    """
    try:
        if isinstance(image_input, str):
            # Load with PIL for resizing if needed
            pil_img = PILImage.open(image_input)
        else:
            pil_img = PILImage.open(io.BytesIO(image_input))
            
        pil_img = _resize_if_needed(pil_img, max_dimension)
        
        # Convert to Bytes for FastMCP
        buffered = io.BytesIO()
        # Default to PNG if possible, or preserve original format if reachable
        fmt = "PNG"
        if not isinstance(image_input, str) and "/" in mime_type:
            fmt = mime_type.split("/")[-1].upper()
            if fmt == "JPG": fmt = "JPEG"
            
        pil_img.save(buffered, format=fmt)
        img_bytes = buffered.getvalue()
        
        return [Image(data=img_bytes, format=fmt.lower()).to_image_content()]
            
    except Exception as e:
        logger.error(f"Error rendering image: {e}")
        return [f"Error rendering image: {str(e)}"]

async def convert_html_to_pdf(html_content: str) -> bytes:
    """
    Renders HTML content to PDF bytes using WeasyPrint.
    """
    try:
        import weasyprint
    except ImportError:
        raise ImportError("weasyprint is required for PDF rendering.")

    # Render to PDF bytes
    # Use asyncio.to_thread because weasyprint can be slow/blocking
    return await asyncio.to_thread(
        weasyprint.HTML(string=html_content).write_pdf, presentational_hints=True
    )

def convert_pdf_to_images(
    pdf_input: Union[bytes, str],
    first_page: int = None,
    last_page: int = None,
    fmt: str = "png"
) -> list:
    """
    Helper to convert PDF (bytes or path) to PIL Images using pdf2image.
    Wraps the bytes vs path distinction and error handling.
    """
    try:
        common_args = {
            "fmt": fmt,
        }
        if first_page:
            common_args["first_page"] = first_page
        if last_page:
            common_args["last_page"] = last_page
            
        if isinstance(pdf_input, str):
            images = convert_from_path(pdf_input, **common_args)
        else:
            images = convert_from_bytes(pdf_input, **common_args)
            
        return images
    except Exception as e:
        # Re-raise with a clear error or handle? 
        # The caller usually expects specific behavior, but let's log and re-raise for now
        # or return empty list? Existing code in render_document_page catches exceptions.
        # existing code in gmail_tools.py raises RuntimeError.
        # Let's let the exception propagate so callers can handle it consistently.
        raise RuntimeError(f"Failed to convert PDF to Image: {e}")

def render_document_page(
    pdf_input: Union[bytes, str], 
    page_number: int = 1,
    max_dimension: int = None
) -> list:
    """
    Renders a specific page of a PDF Document to a PNG image.
    
    Args:
        pdf_input: PDF content as bytes or a file path string.
        page_number: 1-based page number to render.
        max_dimension: Optional maximum width or height.
        
    Returns:
        List containing FastMCP Image object and a text string.
    """
    try:
        # Get total pages first to validate request
        if isinstance(pdf_input, str):
            info = pdfinfo_from_path(pdf_input)
        else:
            info = pdfinfo_from_bytes(pdf_input)
            
        total_pages = int(info.get("Pages", 0))

        if page_number > total_pages:
             return [f"Error: Page {page_number} does not exist. The document has {total_pages} pages."]

        # Convert specific page
        images = convert_pdf_to_images(
            pdf_input, 
            first_page=page_number, 
            last_page=page_number
        )

        if not images:
            return [
                f"Error: Could not render page {page_number}. The document might be empty or invalid."
            ]

        # Get the single image
        image = _resize_if_needed(images[0], max_dimension)
        
        # Convert PIL Image to Bytes
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_bytes = buffered.getvalue()
        
        # Construct response
        footer_text = f"Displaying Page {page_number} of {total_pages}."
        if page_number < total_pages:
             footer_text += f" Call this tool again with page_number={page_number + 1} to see the next page."
        
        return [
            Image(data=img_bytes, format="png").to_image_content(),
            footer_text
        ]

    except Exception as e:
        logger.error(f"Error rendering document page: {e}")
        return [
            f"Error rendering document: {str(e)}"
        ]
