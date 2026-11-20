"""Broker-side, exact task-material authorization.

Task grants are deliberately an additional route to a *fixed reference*, not a
replacement for the raw registry, reviewed-tree registry or derived-data ACL.
The granting teacher must still have a valid source-side right whenever a
delegated reference is used.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .files import KitError
from .web_dataset_trees import is_tree_reference
from .web_datasets import DatasetRegistry
from .web_identity import Principal
from .web_store import _validate_data_references


def _key(reference: dict[str, object]) -> str:
    return json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _identity(principal: object) -> tuple[int, str, str]:
    if not isinstance(principal, Principal):
        raise KitError('任务数据授权需要可信 Principal')
    if (isinstance(principal.uid, bool) or not isinstance(principal.uid, int) or principal.uid < 1 or
            not isinstance(principal.username, str) or not principal.username or
            principal.role not in {'teacher', 'student'} or
            (principal.role == 'teacher') != (principal.username == 'ly')):
        raise KitError('任务数据授权身份无效')
    return principal.uid, principal.username, principal.role


class TaskAccess:
    """Resolve direct or active task-delegated rights without trusting HTTP IDs."""

    def __init__(self, store: object, raw_registry: object, tree_registry: object, derived: object):
        if not hasattr(store, '_connection'):
            raise TypeError('TaskAccess requires the protected submission store')
        self.store = store
        self.raw = raw_registry
        self.trees = tree_registry
        self.derived = derived

    def _tables_exist(self) -> bool:
        with self.store._connection() as connection:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                                      "('web_task_grants','web_task_materials','web_task_deliveries','web_task_recipients')").fetchall()
        return len(rows) == 4

    def _derived_record(self, reference: dict[str, object]):
        """Return the exact derived row if this ID belongs to that namespace."""
        if not getattr(self.derived, 'enabled', False):
            return None
        try:
            with self.store._connection() as connection:
                row = connection.execute('SELECT * FROM web_derived_datasets WHERE dataset_id=?',
                                         (reference['dataset_id'],)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return None
            raise
        if row is None:
            return None
        actual = {'dataset_id': row['dataset_id'], 'path': 'data.bin', 'size': row['size'], 'sha256': row['sha256']}
        if actual != reference:
            raise KitError('派生数据引用不匹配')
        return row

    def _direct(self, principal: Principal, reference: dict[str, object]) -> bool:
        """Ask the authoritative source to validate the exact reference."""
        try:
            if self._derived_record(reference) is not None:
                return any(reference in item.get('files', []) for item in self.derived.catalog(principal))
            if is_tree_reference(reference):
                self.trees.authorize([principal], [reference])
            else:
                self.raw.authorize([principal], [reference])
        except KitError:
            return False
        return True

    def _active_delegations(self, principal: Principal, reference: dict[str, object]) -> list[Principal]:
        uid, username, _role = _identity(principal)
        sponsors: list[Principal] = []
        seen: set[tuple[int, str]] = set()
        rows = []
        if self._tables_exist():
            try:
                with self.store._connection() as connection:
                    rows = connection.execute('''SELECT tasks.teacher_uid,tasks.teacher_username,materials.metadata_json
                    FROM web_task_grants grants
                    JOIN web_task_materials materials ON materials.id=grants.material_id
                    JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
                    JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
                      AND recipients.target_uid=grants.target_uid AND recipients.target_username=grants.target_username
                    WHERE grants.target_uid=? AND grants.target_username=? AND grants.grant_kind='dataset'
                      AND grants.revoked_at IS NULL
                      AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                      AND materials.kind='dataset' ''', (uid, username)).fetchall()
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                rows = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata_json'])
                material_reference = _validate_data_references([metadata['reference']])[0]
            except (KeyError, TypeError, ValueError, KitError) as exc:
                raise KitError('任务数据材料记录已损坏') from exc
            if material_reference != reference:
                continue
            sponsor = Principal(row['teacher_uid'], row['teacher_username'], 'teacher')
            _identity(sponsor)
            identity = sponsor.uid, sponsor.username
            if identity not in seen:
                seen.add(identity)
                sponsors.append(sponsor)
        try:
            with self.store._connection() as connection:
                crows = connection.execute('''SELECT grantor_uid, grantor_username, reference_json
                    FROM web_case_cascaded_grants
                    WHERE resource_kind='dataset' AND resource_ref=?
                      AND (public=1 OR (target_uid=? AND target_username=?))''',
                    (reference['dataset_id'], uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                crows = []
            else:
                raise
        try:
            with self.store._connection() as connection:
                prows = connection.execute('''SELECT grantor_uid, grantor_username, reference_json
                    FROM web_preserved_dataset_grants
                    WHERE resource_ref=?
                      AND (public=1 OR (target_uid=? AND target_username=?))''',
                    (reference['dataset_id'], uid, username)).fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                prows = []
            else:
                raise
        crows = list(crows) + list(prows)
        for crow in crows:
            if crow['reference_json'] and crow['reference_json'] != '{}':
                try:
                    cref = _validate_data_references([json.loads(crow['reference_json'])])[0]
                    if cref != reference:
                        continue
                except (ValueError, TypeError, KitError):
                    continue
            sponsor = Principal(crow['grantor_uid'], crow['grantor_username'], 'teacher')
            _identity(sponsor)
            identity = sponsor.uid, sponsor.username
            if identity not in seen:
                seen.add(identity)
                sponsors.append(sponsor)
        return sponsors

    def _sponsor_can_delegate(self, sponsor: Principal, reference: dict[str, object]) -> bool:
        record = self._derived_record(reference)
        if record is None:
            return self._direct(sponsor, reference)
        # A reviewer may inspect private student output, but that review grant
        # must never become a transitive student-sharing capability.
        return (record['visibility'] == 'public' or
                (record['owner_uid'], record['owner_username']) == (sponsor.uid, sponsor.username))

    def principal_for(self, principal: Principal, reference: object) -> Principal:
        normalized = _validate_data_references([reference])[0]
        if self._direct(principal, normalized):
            return principal
        for sponsor in self._active_delegations(principal, normalized):
            if self._sponsor_can_delegate(sponsor, normalized):
                return sponsor
        raise KitError('无权使用任务数据：不存在有效的精确任务授权')

    def catalog(self, principal: Principal) -> list[dict[str, object]]:
        """Union direct source catalogues with still-valid exact task materials."""
        _identity(principal)
        direct = list(self.raw.catalog(principal)) + list(self.trees.catalog(principal)) + list(self.derived.catalog(principal))
        merged: dict[str, dict[str, object]] = {}
        for item in direct:
            dataset_id = str(item['dataset_id'])
            merged[dataset_id] = {**item, 'files': [dict(entry) for entry in item.get('files', [])]}
        # Even if web_task_* tables do not exist yet, case-cascaded grants may exist.
        uid, username, _role = _identity(principal)
        with self.store._connection() as connection:
            if self._tables_exist():
                rows = connection.execute('''SELECT materials.metadata_json FROM web_task_grants grants
                    JOIN web_task_materials materials ON materials.id=grants.material_id
                    JOIN web_task_deliveries tasks ON tasks.id=grants.task_id
                    JOIN web_task_recipients recipients ON recipients.task_id=grants.task_id
                      AND recipients.target_uid=grants.target_uid AND recipients.target_username=grants.target_username
                    WHERE grants.target_uid=? AND grants.target_username=? AND grants.grant_kind='dataset'
                      AND grants.revoked_at IS NULL
                      AND (tasks.delivery_mode='resources' OR recipients.distribution_id IS NOT NULL)
                      AND materials.kind='dataset' ''', (uid, username)).fetchall()
            else:
                rows = []
            try:
                crows = connection.execute('''SELECT reference_json FROM web_case_cascaded_grants
                    WHERE resource_kind='dataset' AND (public=1 OR (target_uid=? AND target_username=?))''',
                    (uid, username)).fetchall()
            except sqlite3.OperationalError as exc:
                if 'no such table' in str(exc):
                    crows = []
                else:
                    raise
            try:
                prows = connection.execute('''SELECT reference_json FROM web_preserved_dataset_grants
                    WHERE public=1 OR (target_uid=? AND target_username=?)''',
                    (uid, username)).fetchall()
            except sqlite3.OperationalError as exc:
                if 'no such table' in str(exc):
                    prows = []
                else:
                    raise
            crows = list(crows) + list(prows)
        candidate_refs: list[dict[str, object]] = []
        for row in rows:
            try:
                candidate_refs.append(_validate_data_references([json.loads(row['metadata_json'])['reference']])[0])
            except (KeyError, TypeError, ValueError, KitError) as exc:
                raise KitError('任务数据材料记录已损坏') from exc
        for crow in crows:
            if crow['reference_json'] and crow['reference_json'] != '{}':
                try:
                    candidate_refs.append(_validate_data_references([json.loads(crow['reference_json'])])[0])
                except (ValueError, TypeError, KitError):
                    continue
        for reference in candidate_refs:
            try:
                self.principal_for(principal, reference)
            except KitError:
                continue  # sender lost source access; do not advertise stale delegation
            item = merged.setdefault(str(reference['dataset_id']), {'dataset_id': reference['dataset_id'], 'files': []})
            files = item['files']
            assert isinstance(files, list)
            if reference not in files:
                files.append(dict(reference))
        return sorted(merged.values(), key=lambda item: str(item['dataset_id']))

    def validate_transfer(self, sender: Principal, targets: object, references: object) -> list[dict[str, object]]:
        """Check that a sender can safely offer these exact immutable references."""
        _identity(sender)
        if type(targets) is not list or not targets:
            raise KitError('任务接收者必须是可信 Principal 列表')
        checked_targets: list[Principal] = []
        for target in targets:
            _identity(target)
            checked_targets.append(target)
        refs = _validate_data_references(references)
        for reference in refs:
            record = self._derived_record(reference)
            if record is None:
                if not self._direct(sender, reference):
                    raise KitError('发送者无权委托该原始或目录数据')
                continue
            own = (record['owner_uid'], record['owner_username']) == (sender.uid, sender.username)
            public = record['visibility'] == 'public'
            for target in checked_targets:
                if self._direct(target, reference):
                    continue
                if not (public or own):
                    raise KitError('派生数据仅可由所有者或公开数据转授；教师审核权限不可转授')
        return refs

    def authorize_job(self, owner: Principal, requester: Principal, submission_id: object,
                      references: object) -> dict[str, object]:
        """Build runner-only per-reference principals and derived mount claims."""
        _identity(owner); _identity(requester)
        if not isinstance(submission_id, str):
            raise KitError('固定提交 ID 无效')
        refs = _validate_data_references(references)
        derived_mounts: list[dict[str, object]] = []
        authorizations: list[dict[str, object]] = []
        for reference in refs:
            owner_effective = self.principal_for(owner, reference)
            requester_effective = self.principal_for(requester, reference)
            identities: list[Principal] = []
            for principal in (owner_effective, requester_effective):
                if (principal.uid, principal.username) not in {(item.uid, item.username) for item in identities}:
                    identities.append(principal)
            if self._derived_record(reference) is not None:
                mounts = self.derived.claim_mounts(owner_effective, requester_effective, submission_id, [reference])
                if len(mounts) != 1 or mounts[0].get('reference') != reference:
                    raise KitError('派生数据任务授权返回无效')
                derived_mounts.extend(mounts)
            else:
                # Validate each exact source entry now. The runner repeats the
                # source validation with only this broker-issued principal set.
                if is_tree_reference(reference):
                    self.trees.authorize(identities, [reference])
                else:
                    self.raw.authorize(identities, [reference])
                authorizations.append({'reference': dict(reference), 'principals': [
                    {'uid': item.uid, 'username': item.username, 'role': item.role} for item in identities]})
        return {'derived_mounts': derived_mounts, 'dataset_authorizations': authorizations}
