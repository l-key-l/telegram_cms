import hashlib
import json
from pathlib import Path
import secrets
import tempfile
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from channel_manager.models import AuditEvent
from scripts.server_incident_response import (
    SHANGHAI, export_evidence, manifest, private_output, reset_admin, select_admin,
)


class IncidentResponseTests(TestCase):
    def setUp(self):
        self.old_password = secrets.token_urlsafe(24)
        self.new_password = secrets.token_urlsafe(24)
        self.master = User.objects.create_user(username='incident-master', password=self.old_password, role='MASTER')
        self.sub = User.objects.create_user(username='incident-sub', password=secrets.token_urlsafe(24), role='SUB', parent=self.master)

    def test_reset_preserves_user_and_subaccounts_revokes_only_target_sessions(self):
        master_client, sub_client = Client(), Client()
        master_client.force_login(self.master)
        sub_client.force_login(self.sub)
        old_session = master_client.session.session_key
        sub_session = sub_client.session.session_key
        old_auth_hash = self.master.get_session_auth_hash()
        result = reset_admin(self.master.pk, 'secured-master', self.new_password)
        self.master.refresh_from_db()
        self.sub.refresh_from_db()
        self.assertEqual(self.master.username, 'secured-master')
        self.assertEqual(self.sub.parent_id, self.master.pk)
        self.assertFalse(Session.objects.filter(pk=old_session).exists())
        self.assertTrue(Session.objects.filter(pk=sub_session).exists())
        self.assertNotEqual(self.master.get_session_auth_hash(), old_auth_hash)
        self.assertIsNone(authenticate(username='incident-master', password=self.old_password))
        self.assertEqual(authenticate(username='secured-master', password=self.new_password).pk, self.master.pk)
        self.assertEqual(result['revoked_sessions'], 1)
        audit = AuditEvent.objects.get(action='security.master.credentials.rotate')
        self.assertEqual(audit.before_json, {'username': 'incident-master'})
        self.assertNotIn(self.new_password, json.dumps(audit.after_json))

    def test_weak_password_duplicate_username_and_subaccount_leave_data_unchanged(self):
        old_hash = self.master.password
        with self.assertRaises(ValidationError):
            reset_admin(self.master.pk, 'secured-master', '123456')
        with self.assertRaises(ValidationError):
            reset_admin(self.master.pk, self.sub.username, self.new_password)
        with self.assertRaises(ValueError):
            reset_admin(self.sub.pk, 'wrong-target', self.new_password)
        self.master.refresh_from_db()
        self.assertEqual(self.master.password, old_hash)
        self.assertEqual(self.master.username, 'incident-master')
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_audit_failure_rolls_back_reset_and_session_revocation(self):
        self.client.force_login(self.master)
        session_key = self.client.session.session_key
        old_hash = self.master.password
        with patch('channel_manager.audit.audit_event', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                reset_admin(self.master.pk, 'secured-master', self.new_password)
        self.master.refresh_from_db()
        self.assertEqual(self.master.password, old_hash)
        self.assertEqual(self.master.username, 'incident-master')
        self.assertTrue(Session.objects.filter(pk=session_key).exists())

    def test_ambiguous_admin_selection_requires_explicit_id(self):
        self.assertEqual(select_admin(None).pk, self.master.pk)
        User.objects.create_user(username='other-master', role='MASTER', password=secrets.token_urlsafe(24))
        with self.assertRaises(ValueError):
            select_admin(None)
        self.assertEqual(select_admin(self.master.pk).pk, self.master.pk)
        with self.assertRaises(ValueError):
            select_admin(self.sub.pk)

    def test_export_is_read_only_redacts_secrets_and_hashes_evidence(self):
        self.client.force_login(self.master)
        key = self.client.session.session_key
        fake_secret = secrets.token_urlsafe(32)
        event = AuditEvent.objects.create(action='telegram.channel.update.enqueue', outcome='ACCEPTED',
                                         object_type='channel', after_json={'title': 'changed', 'token': fake_secret})
        old_hash = self.master.password
        with tempfile.TemporaryDirectory() as tmp:
            output = private_output(Path(tmp) / 'evidence')
            day = timezone.now().astimezone(SHANGHAI).date()
            summary = export_evidence(output, day, [])
            manifest(output)
            self.assertEqual(summary['channel_change_requests'], 1)
            data = (output / 'audit.jsonl').read_text(encoding='utf-8')
            self.assertIn(str(event.pk), data)
            self.assertNotIn(fake_secret, data)
            for entry in json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['files']:
                content = (output / entry['path']).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry['sha256'])
                self.assertNotIn(key.encode(), content)
                self.assertNotIn(old_hash.encode(), content)
        self.master.refresh_from_db()
        self.assertEqual(self.master.password, old_hash)
        self.assertTrue(Session.objects.filter(pk=key).exists())

    def test_output_rejects_webroot_and_existing_directory(self):
        from django.conf import settings
        with self.assertRaises(ValueError):
            private_output(Path(settings.BASE_DIR) / 'public-evidence')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileExistsError):
                private_output(tmp)
