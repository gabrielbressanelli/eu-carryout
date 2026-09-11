from uuid import uuid4

from django import forms


def logo_upload_path(instance, filename):
    return f"restaurants/{instance.media_key}/branding/{uuid4().hex}.{filename.rsplit('.', 1)[-1].lower()}"


def menu_upload_path(instance, filename):
    return f"restaurants/{instance.tenant.media_key}/menu/{uuid4().hex}.{filename.rsplit('.', 1)[-1].lower()}"


class RestaurantImageField(forms.ImageField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp", "data-image-input": "", "class": "upload-input"}))
        super().__init__(**kwargs)

    def to_python(self, data):
        if data and getattr(data, "size", 0) > 5 * 1024 * 1024:
            raise forms.ValidationError("Choose an image smaller than 5 MB.")
        image = super().to_python(data)
        if image:
            extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(image.image.format)
            if not extension:
                raise forms.ValidationError("Choose a JPG, PNG, or WebP image.")
            if image.image.width * image.image.height > 25_000_000:
                raise forms.ValidationError("Choose an image with fewer than 25 million pixels.")
            image.name = f"{uuid4().hex}.{extension}"
        return image
