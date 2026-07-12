from marcus_code.task_contract import (
    TaskKind,
    derive_task_contract,
    is_verification_evidence,
)


def test_explanation_does_not_require_plan_or_verification():
    contract = derive_task_contract("ทำไมระบบนี้ถึงค้าง")

    assert contract.kind == TaskKind.explain
    assert contract.requires_plan is False
    assert contract.requires_verification is False


def test_implementation_with_test_requires_plan_and_verification():
    contract = derive_task_contract("ช่วย implement API แล้ว run curl ยืนยันผล")

    assert contract.kind == TaskKind.change
    assert contract.requires_plan is True
    assert contract.requires_verification is True


def test_successful_curl_is_verification_evidence():
    assert is_verification_evidence(
        "run_cli", {"command": "curl http://localhost:5000"}, {"exit_code": 0}
    )
    assert not is_verification_evidence(
        "run_cli", {"command": "curl http://localhost:5000"}, {"exit_code": 1}
    )
