from marcus_code.task_contract import (
    Capability,
    ResponseMode,
    TaskKind,
    VerificationPolicy,
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


def test_general_knowledge_question_uses_direct_mode_without_capabilities():
    contract = derive_task_contract("ช่วยอธิบาย JWT แบบสั้นๆ")

    assert contract.response_mode == ResponseMode.direct
    assert contract.capabilities == frozenset()
    assert contract.requires_plan is False


def test_general_programming_questions_do_not_become_workspace_operations():
    for prompt in ("What is a Python function?", "How do I run pytest?", "อธิบาย unit test"):
        contract = derive_task_contract(prompt)
        assert contract.response_mode == ResponseMode.direct
        assert contract.capabilities == frozenset()


def test_short_workspace_commands_enter_agentic_read_mode():
    for prompt in ("find the bug in src/app.py", "list files", "show git status"):
        contract = derive_task_contract(prompt)
        assert contract.response_mode == ResponseMode.agentic
        assert Capability.workspace_read in contract.capabilities


def test_plan_only_request_is_read_only_even_when_it_mentions_improvement():
    contract = derive_task_contract(
        "ช่วยวิเคราะห์ code และวางแผนปรับปรุง แต่ยังไม่ต้องแก้โค้ด"
    )

    assert contract.kind == TaskKind.explain
    assert contract.response_mode == ResponseMode.agentic
    assert contract.capabilities == frozenset({Capability.workspace_read})
    assert contract.requires_plan is True
    assert contract.verification_policy == VerificationPolicy.none


def test_compound_change_operation_unions_required_capabilities():
    contract = derive_task_contract("ช่วยแก้ API แล้วรัน service และ curl ยืนยันผล")

    assert contract.capabilities == frozenset(
        {
            Capability.workspace_read,
            Capability.workspace_write,
            Capability.command,
            Capability.process,
        }
    )
    assert contract.verification_policy == VerificationPolicy.after_mutation


def test_thai_test_request_has_command_capability():
    contract = derive_task_contract("ช่วยทดสอบโปรเจกต์นี้")

    assert contract.kind == TaskKind.operate
    assert Capability.command in contract.capabilities
    assert contract.verification_policy == VerificationPolicy.requested


def test_run_tests_is_evidence_and_command_substrings_are_not():
    assert is_verification_evidence("run_tests", {}, {"exit_code": 0, "stdout": "2 passed"})
    assert not is_verification_evidence(
        "run_cli", {"command": "echo latest"}, {"exit_code": 0}
    )
