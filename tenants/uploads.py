from io import BytesIO
from uuid import uuid4

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.text import slugify
from PIL import Image, ImageOps


TARGET_IMAGE_BYTES = 150 * 1024
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
QUALITY_STEPS = (85, 80, 75, 70, 65, 60, 55, 50, 45, 40)
SCALE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.42, 0.32, 0.24, 0.18, 0.14)
MAX_EDGES = {"logo": 800, "menu_item": 1200}


def logo_upload_path(instance, filename):
    return f"carryout/{_path_slug(instance.account.slug)}/{_path_slug(instance.slug)}/logo/{uuid4().hex}.{_extension(filename)}"


def menu_upload_path(instance, filename):
    item_slug = slugify(instance.name)[:60] or "menu-item"
    return f"carryout/{_path_slug(instance.tenant.account.slug)}/{_path_slug(instance.tenant.slug)}/menu-items/{item_slug}-{uuid4().hex}.{_extension(filename)}"


def _extension(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "webp"


def _path_slug(value):
    return slugify(value) or "unnamed"


def _has_alpha(image):
    if image.mode in {"RGBA", "LA"}:
        return True
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False


def _resize_to_max(image, max_edge):
    resized = image.copy()
    resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return resized


def _resize_by_scale(image, scale):
    if scale == 1.0:
        return image.copy()
    width = max(96, int(image.width * scale))
    height = max(96, int(image.height * scale))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode(image, image_format, quality=None):
    output = BytesIO()
    if image_format == "PNG":
        prepared = image.convert("RGBA") if _has_alpha(image) else image.convert("RGB")
        prepared.save(output, format="PNG", optimize=True)
        content_type = "image/png"
        extension = "png"
    else:
        prepared = image.convert("RGBA") if _has_alpha(image) else image.convert("RGB")
        prepared.save(output, format="WEBP", quality=quality, method=6)
        content_type = "image/webp"
        extension = "webp"
    return output.getvalue(), content_type, extension


def _candidate_formats(kind, image):
    if kind == "logo" and _has_alpha(image):
        return ("PNG", "WEBP")
    return ("WEBP",)


def optimize_upload(upload, kind):
    upload.seek(0)
    source = Image.open(upload)
    source.load()
    source = ImageOps.exif_transpose(source)
    base = _resize_to_max(source, MAX_EDGES.get(kind, MAX_EDGES["menu_item"]))
    best = None

    for scale in SCALE_STEPS:
        candidate = _resize_by_scale(base, scale)
        for image_format in _candidate_formats(kind, candidate):
            qualities = (None,) if image_format == "PNG" else QUALITY_STEPS
            for quality in qualities:
                content, content_type, extension = _encode(candidate, image_format, quality)
                if best is None or len(content) < len(best[0]):
                    best = (content, content_type, extension)
                if len(content) <= TARGET_IMAGE_BYTES:
                    return SimpleUploadedFile(
                        f"{uuid4().hex}.{extension}",
                        content,
                        content_type=content_type,
                    )

    if best and len(best[0]) <= TARGET_IMAGE_BYTES:
        content, content_type, extension = best
        return SimpleUploadedFile(f"{uuid4().hex}.{extension}", content, content_type=content_type)
    raise forms.ValidationError("This image could not be optimized below 150 KB. Try a simpler or smaller image.")


class RestaurantImageField(forms.ImageField):
    def __init__(self, *, kind="menu_item", **kwargs):
        self.kind = kind
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp", "data-image-input": "", "class": "upload-input"}))
        super().__init__(**kwargs)

    def to_python(self, data):
        if data and getattr(data, "size", 0) > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Choose an image smaller than 5 MB.")
        image = super().to_python(data)
        if image:
            if image.image.format not in ALLOWED_IMAGE_FORMATS:
                raise forms.ValidationError("Choose a JPG, PNG, or WebP image.")
            if image.image.width * image.image.height > MAX_IMAGE_PIXELS:
                raise forms.ValidationError("Choose an image with fewer than 25 million pixels.")
            image = optimize_upload(image, self.kind)
        return image
