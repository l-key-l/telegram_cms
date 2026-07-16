from __future__ import annotations

import hashlib
import math
import mimetypes
import uuid
from io import BytesIO
from pathlib import Path

from django.core.files.base import ContentFile as DjangoContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps
from opencc import OpenCC

from accounts.models import User

from .models import (
    Content,
    ContentChannel,
    ContentFile,
    ContentSequence,
    Operation,
    TelegramChannel,
)
from .worker_signal import notify_operation_worker


_S2T_CONVERTER = OpenCC("s2t")
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
TELEGRAM_PHOTO_TARGET_BYTES = 9 * 1024 * 1024
TELEGRAM_PHOTO_MAX_SIDE_SUM = 10000
TELEGRAM_PHOTO_SAFE_SIDE_SUM = 9800
TELEGRAM_PHOTO_MAX_RATIO = 20


def render_content_text(content: Content) -> str:
    """按内容配置生成真正发送给 Telegram 的文字，数据库保留用户原始输入。"""
    text = content.text or ""
    if content.text_variant == Content.TextVariant.TRADITIONAL:
        return _S2T_CONVERTER.convert(text)
    return text


def next_content_identity(owner: User) -> tuple[str, str]:
    with transaction.atomic():
        sequence, _ = ContentSequence.objects.select_for_update().get_or_create(owner_user=owner)
        value = sequence.next_value
        sequence.next_value += 1
        sequence.save(update_fields=("next_value",))
    code = str(value)
    return code, f"{owner.label}.{code}"


def detect_media_type(filename: str, content_type: str = "") -> str:
    mime = content_type or mimetypes.guess_type(filename)[0] or ""
    if mime.startswith("image/"):
        return ContentFile.MediaType.PHOTO
    if mime.startswith("video/"):
        return ContentFile.MediaType.VIDEO
    return ContentFile.MediaType.DOCUMENT


def checksum(uploaded_file) -> str:
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def attach_files(content: Content, uploaded_files, *, group_no: int, start_index: int = 0):
    for offset, uploaded in enumerate(uploaded_files):
        item = ContentFile.objects.create(
            content=content,
            group_no=group_no,
            sort_index=start_index + offset,
            media_type=detect_media_type(uploaded.name, getattr(uploaded, "content_type", "")),
            file=uploaded,
            sha256=checksum(uploaded),
            mime=getattr(uploaded, "content_type", "") or mimetypes.guess_type(uploaded.name)[0] or "",
            size=uploaded.size,
        )
        if item.media_type == ContentFile.MediaType.PHOTO:
            ensure_telegram_safe_photo(item, anti_scan_enabled=content.anti_scan_enabled)


def _create_processed_image(item: ContentFile):
    ensure_telegram_safe_photo(item, anti_scan_enabled=True, force=True)


def _image_to_rgb(source: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(source)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        alpha = image.getchannel("A") if image.mode in {"RGBA", "LA"} else None
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image.convert("RGBA"), mask=alpha or image.convert("RGBA").getchannel("A"))
        return background
    return image.convert("RGB")


def _photo_needs_normalization(width: int, height: int, size: int = 0) -> bool:
    if width <= 0 or height <= 0:
        return True
    ratio = max(width, height) / max(1, min(width, height))
    return width + height > TELEGRAM_PHOTO_MAX_SIDE_SUM or ratio > TELEGRAM_PHOTO_MAX_RATIO or size > TELEGRAM_PHOTO_MAX_BYTES


def _is_telegram_safe_photo(path: str) -> bool:
    try:
        with Image.open(path) as source:
            width, height = ImageOps.exif_transpose(source).size
        return not _photo_needs_normalization(width, height, Path(path).stat().st_size)
    except (OSError, ValueError):
        return False


def _build_telegram_safe_photo(source: Image.Image, *, anti_scan_enabled: bool) -> Image.Image:
    image = _image_to_rgb(source)
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸为空")

    ratio = max(width, height) / max(1, min(width, height))
    if ratio > TELEGRAM_PHOTO_MAX_RATIO:
        if width >= height:
            target_height = max(height, math.ceil(width / TELEGRAM_PHOTO_MAX_RATIO))
            canvas = Image.new("RGB", (width, target_height), (255, 255, 255))
            canvas.paste(image, (0, (target_height - height) // 2))
        else:
            target_width = max(width, math.ceil(height / TELEGRAM_PHOTO_MAX_RATIO))
            canvas = Image.new("RGB", (target_width, height), (255, 255, 255))
            canvas.paste(image, ((target_width - width) // 2, 0))
        image = canvas
        width, height = image.size

    side_sum = width + height
    if side_sum > TELEGRAM_PHOTO_SAFE_SIDE_SUM:
        scale = TELEGRAM_PHOTO_SAFE_SIDE_SUM / side_sum
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)

    if anti_scan_enabled and image.width and image.height:
        x, y = image.width - 1, image.height - 1
        red, green, blue = image.getpixel((x, y))
        image.putpixel((x, y), ((red + 1) % 256, green, blue))
    return image


def _encoded_safe_photo_bytes(image: Image.Image) -> bytes:
    current = image
    quality = 92
    for _ in range(8):
        output = BytesIO()
        current.save(output, format="JPEG", quality=quality, optimize=True)
        data = output.getvalue()
        if len(data) <= TELEGRAM_PHOTO_TARGET_BYTES:
            return data
        if quality > 76:
            quality -= 8
            continue
        width, height = current.size
        scale = max(0.55, min(0.92, math.sqrt(TELEGRAM_PHOTO_TARGET_BYTES / len(data)) * 0.96))
        current = current.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
    return data


def ensure_telegram_safe_photo(item: ContentFile, *, anti_scan_enabled: bool | None = None, force: bool = False) -> None:
    if item.media_type != ContentFile.MediaType.PHOTO:
        return
    if anti_scan_enabled is None:
        anti_scan_enabled = bool(getattr(item.content, "anti_scan_enabled", False))
    if item.processed_file and not force and _is_telegram_safe_photo(item.processed_file.path):
        return
    if not force and not anti_scan_enabled and _is_telegram_safe_photo(item.file.path):
        return
    try:
        with Image.open(item.file.path) as source:
            image = _build_telegram_safe_photo(source, anti_scan_enabled=anti_scan_enabled)
            data = _encoded_safe_photo_bytes(image)
            item.processed_file.save("telegram_safe.jpg", DjangoContentFile(data), save=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{item.file.name} 图片预处理失败：{exc}") from exc


def telegram_media_source_path(item: ContentFile, *, anti_scan_enabled: bool | None = None) -> str:
    if item.media_type == ContentFile.MediaType.PHOTO:
        ensure_telegram_safe_photo(item, anti_scan_enabled=anti_scan_enabled)
        if item.processed_file:
            return item.processed_file.path
    return item.file.path


def sync_content_channels(content: Content, channels):
    ContentChannel.objects.filter(content=content).exclude(channel__in=channels).delete()
    existing = set(ContentChannel.objects.filter(content=content).values_list("channel_id", flat=True))
    ContentChannel.objects.bulk_create(
        [ContentChannel(content=content, channel=channel) for channel in channels if channel.pk not in existing],
        ignore_conflicts=True,
    )


def enqueue_content_operation(
    *,
    content: Content,
    action: str,
    actor,
    source: str = Operation.Source.WEB,
    request_json: dict | None = None,
    idempotency_key: str | None = None,
) -> tuple[Operation, bool]:
    marker = (idempotency_key or uuid.uuid4().hex).strip()[:64]
    operation_key = f"{action}:{content.pk}:{content.version}:{marker}"
    return enqueue_operation(
        owner_user=content.owner_user,
        actor=actor,
        source=source,
        action=action,
        target_type="content",
        target_id=str(content.pk),
        request_json=request_json,
        idempotency_key=operation_key,
    )


def enqueue_operation(
    *,
    owner_user: User,
    actor,
    source: str,
    action: str,
    target_type: str,
    target_id: str,
    request_json: dict | None = None,
    idempotency_key: str | None = None,
) -> tuple[Operation, bool]:
    operation_key = (idempotency_key or f"{action}:{target_type}:{target_id}:{uuid.uuid4().hex}")[:120]
    operation, created = Operation.objects.get_or_create(
        owner_user=owner_user,
        idempotency_key=operation_key,
        defaults={
            "actor_type": (
                Operation.ActorType.TELEGRAM_USER
                if source == Operation.Source.TELEGRAM
                else Operation.ActorType.SYSTEM
                if source in {Operation.Source.SCHEDULER, Operation.Source.SYSTEM}
                else Operation.ActorType.WEB_USER
            ),
            "actor_id": str(getattr(actor, "pk", actor or "")),
            "source": source,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "request_json": request_json or {},
            "state": Operation.State.QUEUED,
            "available_at": timezone.now(),
        },
    )
    if created:
        notify_operation_worker()
    return operation, created


def enqueue_due_content_operations(*, limit: int = 20) -> int:
    """把到期内容转成数据库任务；先清空 next_run_at，避免多个 Worker 重复入队。"""
    now = timezone.now()
    queued = 0
    for content_id in (
        Content.objects.filter(next_run_at__isnull=False, next_run_at__lte=now, deleted_at__isnull=True)
        .order_by("next_run_at")
        .values_list("pk", flat=True)[:limit]
    ):
        with transaction.atomic():
            content = Content.objects.select_for_update().filter(
                pk=content_id,
                next_run_at__isnull=False,
                next_run_at__lte=now,
                deleted_at__isnull=True,
            ).first()
            if not content:
                continue
            due_at = content.next_run_at
            content.next_run_at = None
            content.save(update_fields=("next_run_at", "updated_at"))
            action = Operation.Action.REPLACE if content.status in {Content.Status.PUBLISHED, Content.Status.PARTIAL} else Operation.Action.SEND
            operation, created = enqueue_content_operation(
                content=content,
                action=action,
                actor="scheduler",
                source=Operation.Source.SCHEDULER,
                request_json={
                    "due_at": due_at.isoformat(),
                    "force_resend": action == Operation.Action.REPLACE,
                    "reason": "SCHEDULED_UPDATE",
                },
                idempotency_key=f"schedule:{due_at.isoformat()}",
            )
            if created:
                from .audit import audit_event

                audit_event(
                    request=None,
                    actor="scheduler",
                    owner_user=content.owner_user,
                    action="operation.enqueue",
                    object_type="operation",
                    object_id=str(operation.pk),
                    outcome="ACCEPTED",
                    before={"next_run_at": due_at.isoformat()},
                    after={"action": action, "content_id": content.pk},
                    operation=operation,
                    source="SCHEDULER",
                    actor_type="SYSTEM",
                )
            queued += int(created)
    return queued


def available_channels_for(user: User):
    if user.role == User.Role.MASTER:
        return TelegramChannel.objects.all()
    return TelegramChannel.objects.filter(user_accesses__user=user).distinct()
