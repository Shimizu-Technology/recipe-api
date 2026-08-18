"""Central image validation before storage or paid AI calls."""

import base64
import binascii
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_DIMENSION = 12_000
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


class ImageValidationError(ValueError):
    """Raised when image bytes do not satisfy the application contract."""


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    content_type: str
    width: int
    height: int


def decode_and_validate_base64_image(
    encoded: str,
    *,
    max_bytes: int,
) -> ValidatedImage:
    """Decode strict base64 and validate its actual image format and dimensions."""
    if not encoded:
        raise ImageValidationError("Image data is required")

    try:
        image_data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageValidationError("Image must be valid base64") from exc

    return validate_image_bytes(image_data, max_bytes=max_bytes)


def validate_image_bytes(
    image_data: bytes,
    *,
    max_bytes: int,
    declared_content_type: str | None = None,
) -> ValidatedImage:
    """Validate byte size, real MIME type, dimensions, and decompression risk."""
    if not image_data:
        raise ImageValidationError("Image data is required")
    if len(image_data) > max_bytes:
        raise ImageValidationError(f"Image exceeds the {max_bytes // (1024 * 1024)}MB limit")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_data)) as image:
                image_format = (image.format or "").upper()
                content_type = ALLOWED_IMAGE_FORMATS.get(image_format)
                if not content_type:
                    raise ImageValidationError("Unsupported image format")

                width, height = image.size
                if width < 1 or height < 1:
                    raise ImageValidationError("Image dimensions are invalid")
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise ImageValidationError("Image dimensions are too large")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ImageValidationError("Image contains too many pixels")

                image.verify()
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError("Image dimensions are unsafe") from exc
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ImageValidationError("Image data is corrupt or unsupported") from exc

    normalized_declared_type = (declared_content_type or "").split(";", 1)[0].lower()
    if normalized_declared_type == "image/jpg":
        normalized_declared_type = "image/jpeg"
    if normalized_declared_type and normalized_declared_type != content_type:
        raise ImageValidationError("Image content does not match its declared type")

    return ValidatedImage(
        data=image_data,
        content_type=content_type,
        width=width,
        height=height,
    )
