from __future__ import annotations

from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from .models import ContentFile, TelegramChannel


def _delete_field_file(field_file):
    if field_file and field_file.name:
        field_file.storage.delete(field_file.name)


@receiver(post_delete, sender=ContentFile)
def delete_content_files(sender, instance, **kwargs):
    _delete_field_file(instance.file)
    _delete_field_file(instance.processed_file)


@receiver(pre_save, sender=TelegramChannel)
def delete_replaced_channel_photo(sender, instance, **kwargs):
    if not instance.pk:
        return
    previous = sender.objects.filter(pk=instance.pk).only("photo").first()
    if previous and previous.photo and previous.photo.name != instance.photo.name:
        _delete_field_file(previous.photo)


@receiver(post_delete, sender=TelegramChannel)
def delete_channel_photo(sender, instance, **kwargs):
    _delete_field_file(instance.photo)
