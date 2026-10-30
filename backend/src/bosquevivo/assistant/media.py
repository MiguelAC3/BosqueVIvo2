import io

from PIL import Image, UnidentifiedImageError

from bosquevivo.core.errors import ExternalServiceError

MAX_SIDE = 1600


def strip_metadata(data: bytes) -> tuple[bytes, str]:
    """Vuelve a codificar la foto sin EXIF (ubicación, dispositivo, fecha) antes de enviarla
    al modelo. Se reduce a MAX_SIDE píxeles por lado para gastar menos cuota."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            clean = image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ExternalServiceError("el archivo recibido no es una imagen válida") from exc
    clean.thumbnail((MAX_SIDE, MAX_SIDE))
    output = io.BytesIO()
    clean.save(output, format="JPEG", quality=85)
    return output.getvalue(), "image/jpeg"
