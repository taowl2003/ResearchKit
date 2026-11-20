from types import SimpleNamespace
import pytest
from researchkit.files import KitError
from researchkit.web_identity import IdentityError, Principal
from researchkit.web_runner import validate_job_identities

TEACHER=Principal(1001,'ly','teacher')
STUDENT=Principal(1002,'test','student')


def test_teacher_can_review_missing_owner_but_uid_reuse_is_rejected(monkeypatch):
    def principal(uid):
        if uid == TEACHER.uid: return TEACHER
        raise IdentityError('account retired')
    policy=SimpleNamespace(principal=principal)
    job={'owner_uid':STUDENT.uid,'owner_username':'test','requester_uid':TEACHER.uid,'requester_username':'ly'}
    monkeypatch.setattr('pwd.getpwuid', lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    validate_job_identities(policy,job)
    monkeypatch.setattr('pwd.getpwuid', lambda uid: SimpleNamespace(pw_name='replacement'))
    with pytest.raises(KitError,match='复用'):
        validate_job_identities(policy,job)


def test_retired_requester_or_identity_change_never_runs():
    job={'owner_uid':STUDENT.uid,'owner_username':'test','requester_uid':STUDENT.uid,'requester_username':'test'}
    policy=SimpleNamespace(principal=lambda uid: (_ for _ in ()).throw(IdentityError('retired')))
    with pytest.raises(IdentityError): validate_job_identities(policy,job)
    policy=SimpleNamespace(principal=lambda uid: Principal(uid,'different','student'))
    with pytest.raises(KitError,match='变化'):validate_job_identities(policy,job)
