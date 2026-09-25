"""Server-only incident export and explicit, in-place administrator reset.

Run with the deployed project's Python. No migrations, Telegram calls, password
arguments, environment-file copies, or business-data deletions are performed.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import warnings
from datetime import date, datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{25,}\b")
SENSITIVE = re.compile(r"password|passwd|secret|token|cookie|session_data|session_key|authorization|api_hash", re.I)


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SENSITIVE.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return TOKEN_PATTERN.sub("[REDACTED_BOT_TOKEN]", value)
    return value


def write_json(path, value):
    from django.core.serializers.json import DjangoJSONEncoder
    with path.open("x", encoding="utf-8") as f:
        os.chmod(path, 0o600)
        json.dump(sanitize(value), f, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
        f.write("\n")


def write_rows(path, rows):
    from django.core.serializers.json import DjangoJSONEncoder
    count = 0
    with path.open("x", encoding="utf-8") as f:
        os.chmod(path, 0o600)
        for row in rows:
            f.write(json.dumps(sanitize(row), cls=DjangoJSONEncoder, ensure_ascii=False) + "\n")
            count += 1
    return count


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def manifest(output):
    entries = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": file_hash(p)}
               for p in sorted(output.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    write_json(output / "manifest.json", {"created_at_utc": datetime.now(timezone.utc), "files": entries})
    for entry in entries:
        if file_hash(output / entry["path"]) != entry["sha256"]:
            raise RuntimeError("Evidence integrity check failed")


def private_output(path):
    from django.conf import settings
    path = Path(path).expanduser().resolve()
    forbidden = [Path(settings.BASE_DIR).resolve(), Path(settings.MEDIA_ROOT).resolve(), Path('/www/wwwroot').resolve()]
    if any(path == root or root in path.parents for root in forbidden):
        raise ValueError("证据目录请放在网站目录之外，例如 /root/telegram-incident-20260925")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(path, 0o700)
    return path


def session_metadata():
    from django.contrib.sessions.models import Session
    for session in Session.objects.order_by('pk').iterator():
        decoded = session.get_decoded()
        yield {"session_fingerprint": hashlib.sha256(session.session_key.encode()).hexdigest(),
               "user_id": decoded.get('_auth_user_id'), "expire_date": session.expire_date}


def export_evidence(output, day, log_files):
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from channel_manager.models import AuditEvent, Operation, TelegramBot, TelegramChannel, TelegramUserLink

    start = datetime.combine(day, datetime.min.time(), SHANGHAI)
    end = start + timedelta(days=1)
    audited = AuditEvent.objects.filter(created_at__gte=start, created_at__lt=end).order_by('created_at', 'pk')
    operations = Operation.objects.filter(
        Q(created_at__gte=start, created_at__lt=end) | Q(started_at__gte=start, started_at__lt=end)
        | Q(finished_at__gte=start, finished_at__lt=end)).order_by('created_at', 'pk')
    users = list(get_user_model().objects.order_by('pk').values(
        'id', 'username', 'role', 'parent_id', 'is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login'))
    admins = [u for u in users if u['role'] == 'MASTER' or u['is_superuser']]
    counts = {
        'audit_events': write_rows(output / 'audit.jsonl', audited.values().iterator()),
        'operations': write_rows(output / 'operations.jsonl', operations.values().iterator()),
        'session_metadata': write_rows(output / 'sessions-metadata.jsonl', session_metadata()),
    }
    snapshot = {
        'export_started_at_utc': datetime.now(timezone.utc), 'hostname': socket.gethostname(),
        'project_path': str(settings.BASE_DIR), 'db_host': settings.DATABASES['default']['HOST'],
        'db_name': settings.DATABASES['default']['NAME'], 'debug': settings.DEBUG,
        'time_window': {'start_inclusive': start, 'end_exclusive': end},
        'users': users,
        'bots': list(TelegramBot.objects.values('id', 'telegram_bot_id', 'username', 'display_name', 'status', 'updated_at')),
        'channels': list(TelegramChannel.objects.values('id', 'telegram_chat_id', 'title', 'username', 'description', 'updated_at')),
        'telegram_links': list(TelegramUserLink.objects.values('id', 'user_id', 'bot_id', 'telegram_user_id', 'status')),
        'pending_channel_changes': list(Operation.objects.filter(action='UPDATE_CHANNEL', state__in=['QUEUED', 'RUNNING', 'PENDING_CONFIRM']).values('id', 'actor_id', 'created_at', 'target_id', 'state')),
        'notes': [
            'Only this application and explicitly supplied logs; not a complete Telegram-client history.',
            'Audit IP is currently derived from X-Forwarded-For; corroborate with trusted proxy logs.',
            'Live export is not a transactionally consistent full database backup.',
            'Raw log copies are private originals; do not upload them publicly. JSON exports redact sensitive key names.',
        ],
    }
    write_json(output / 'snapshot.json', snapshot)
    copied = []
    for index, name in enumerate(log_files):
        source = Path(name).resolve(strict=True)
        if not source.is_file() or not (source.name.endswith(('.log', '.gz', '.out', '.err')) or '.log.' in source.name):
            raise ValueError(f'请指定实际日志文件：{source.name}')
        target = output / f'raw-log-{index:02d}-{source.name}'
        before = source.stat()
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        after = source.stat()
        copied.append({'source': str(source), 'copy': target.name, 'source_size_before': before.st_size,
                       'source_size_after': after.st_size, 'source_mtime_before': before.st_mtime,
                       'source_mtime_after': after.st_mtime, 'sha256_of_copy': file_hash(target)})
    write_json(output / 'log-sources.json', copied)
    summary = {'window_start': start, 'window_end': end, 'counts': counts, 'admins': admins,
               'login_successes': audited.filter(action='auth.login', outcome='SUCCESS').count(),
               'login_failures': audited.filter(action='auth.login').exclude(outcome='SUCCESS').count(),
               'channel_change_requests': audited.filter(action='telegram.channel.update.enqueue').count(),
               'attribution': '待与代理访问日志、Telegram 最近操作交叉核对；不自动判断入侵者。'}
    write_json(output / 'summary.json', summary)
    return summary


def select_admin(user_id):
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    qs = get_user_model().objects.filter(Q(role='MASTER') | Q(is_superuser=True))
    if user_id is not None:
        qs = qs.filter(pk=user_id)
    matches = list(qs)
    if len(matches) != 1:
        raise ValueError('管理员匹配数量不是 1，请查看 summary.json，再用 --user-id 明确指定。')
    if not matches[0].is_active:
        raise ValueError('目标账号已停用，请先核对账号身份；脚本不会自动启用账号。')
    return matches[0]


def reset_admin(user_id, new_username, password):
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import CommonPasswordValidator, MinimumLengthValidator, NumericPasswordValidator, UserAttributeSimilarityValidator, validate_password
    from django.contrib.sessions.models import Session
    from django.db import transaction
    from channel_manager.audit import audit_event

    if settings.SESSION_ENGINE != 'django.contrib.sessions.backends.db':
        raise ValueError('本脚本针对当前项目的数据库会话；请先核对 SESSION_ENGINE。')
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        if not user.is_active or (user.role != 'MASTER' and not user.is_superuser):
            raise ValueError('目标必须是启用中的管理员。')
        before = {'username': user.username}
        if user.username == new_username or user.check_password(password):
            raise ValueError('请设置与原账号、原密码不同的新凭据。')
        user.username = new_username
        user.full_clean(exclude=['password'])
        validate_password(password, user, [MinimumLengthValidator(16), CommonPasswordValidator(),
                                           NumericPasswordValidator(), UserAttributeSimilarityValidator()])
        user.set_password(password)
        user.save(update_fields=['username', 'password'])
        removed = 0
        for session in Session.objects.iterator():
            if str(session.get_decoded().get('_auth_user_id')) == str(user.pk):
                session.delete()
                removed += 1
        audit = audit_event(request=None, actor='server-incident-cli', owner_user=user,
                            action='security.master.credentials.rotate', object_type='user', object_id=str(user.pk),
                            source='SYSTEM', actor_type='SYSTEM', before=before,
                            after={'username': user.username, 'password_rotated': True, 'revoked_sessions': removed})
        user.refresh_from_db()
        if user.username != new_username or not user.check_password(password):
            raise RuntimeError('凭据写入验证失败，事务回滚。')
        return {'user_id': user.pk, 'old_username': before['username'], 'new_username': user.username,
                'revoked_sessions': removed, 'audit_id': str(audit.pk), 'password_verified': True,
                'completed_at_utc': datetime.now(timezone.utc)}


def execute(args):
    from django.core.exceptions import ValidationError
    output = private_output(args.output)
    summary = export_evidence(output, args.day, args.log_file)
    manifest(output)
    print(json.dumps(sanitize(summary), default=str, ensure_ascii=False, indent=2))
    print(f'已导出并验证证据：{output}')
    if not args.apply:
        print('只读检查完成，未修改账号、密码、会话或业务数据。')
        return
    user = select_admin(args.user_id)
    if not args.new_username:
        raise ValueError('--apply 需要 --new-username；不删除或重建管理员。')
    print(f'将修改管理员 ID={user.pk}，账号 {user.username} -> {args.new_username}')
    # Reject non-TTY fallback rather than accepting a password with echo enabled.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        password = getpass.getpass('输入新密码（至少 16 位，不回显）：')
        repeat = getpass.getpass('再次输入新密码：')
    if password != repeat:
        raise ValueError('两次密码输入不一致，账号未修改。')
    result = reset_admin(user.pk, args.new_username, password)
    del password, repeat
    # Keep pre-reset manifest intact; the extra result is explicitly post-reset.
    try:
        write_json(output / 'reset-result.json', result)
    except OSError:
        print('账号已修改，结果文件写入失败；数据库 security.master.credentials.rotate 审计已保存。', file=sys.stderr)
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
    print('服务器主账号原地更新完成；原主键、子账号归属和频道业务数据保持不变。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=date.fromisoformat, default=datetime.now(SHANGHAI).date(), help='北京时间日期 YYYY-MM-DD')
    parser.add_argument('--output', required=True, type=Path, help='新建私有证据目录，须位于网站外')
    parser.add_argument('--log-file', action='append', default=[], help='额外保全的实际访问/应用日志路径，可重复')
    parser.add_argument('--apply', action='store_true', help='保全证据后交互式修改指定管理员')
    parser.add_argument('--user-id', type=int, help='有多个管理员时必须明确指定')
    parser.add_argument('--new-username', help='新登录名，保留原用户 ID')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'telegram_manager.settings')
    os.environ['EMBEDDED_WORKER'] = '0'
    import django
    django.setup()
    from django.core.exceptions import ValidationError
    try:
        execute(args)
    except (ValueError, ValidationError, OSError, getpass.GetPassWarning, EOFError) as exc:
        # Never include supplied passwords or environment secrets in CLI errors.
        print(f'步骤停止：{type(exc).__name__}；请核对参数、证据目录及输入。详情：{sanitize(str(exc))}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
