"""
System/Local tools for MCP server.
"""
import os
import os
import mimetypes
import logging

from core.server import server
from core.visual_rendering import render_document_page, render_image, convert_html_to_pdf

logger = logging.getLogger(__name__)

@server.tool()
async def get_local_file_visual(path: str, page_number: int = 1, max_dimension: int = None) -> list:
    """
    Visually inspect a local file by rendering it.
    Supports PDF, Images (PNG/JPG), and HTML files.
    
    Args:
        path: Absolute path to the local file.
        page_number: Page number to render (default 1). Ignored for images.
        max_dimension: Optional maximum width or height of the returned image.
        
    Returns:
        list: A list containing FastMCP Image object and text metadata.
    """
    if not os.path.exists(path):
        return [f"Error: File not found at {path}"]
        
    if not os.path.isfile(path):
        return [f"Error: Path is not a file: {path}"]
        
    # Guess mime type
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "application/octet-stream"
        
    try:
        if mime_type.startswith("image/"):
             # Use shared helper for images
             return render_image(path, max_dimension=max_dimension)
            
        elif mime_type == "application/pdf":
            # Delegate to shared renderer (passing path optimizes memory usage)
            return render_document_page(path, page_number, max_dimension=max_dimension)
            
        elif mime_type == "text/html":
             # Render HTML to PDF first
            try:
                with open(path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                pdf_bytes = await convert_html_to_pdf(html_content)
                return render_document_page(pdf_bytes, page_number, max_dimension=max_dimension)
            except Exception as e:
                return [f"Error rendering HTML file: {str(e)}"]

        else:
            return [f"Error: Unsupported file type '{mime_type}'. Only PDF, Images, and HTML are supported for visual inspection."]
            
    except Exception as e:
        logger.error(f"Error reading local file: {e}")
        return [f"Error reading file: {e}"]
