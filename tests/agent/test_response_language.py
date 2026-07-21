from agent.response_language import (
    build_response_language_repair_prompt,
    inspect_response_language,
    normalize_response_language,
    response_language_failure_message,
)


def test_korean_guard_rejects_observed_mixed_scripts():
    text = (
        "문제 원인을 정리합니다. 刚才 vLLM은 정상이지만 "
        "التقرير 출력과 серия 상태가 섞였습니다."
    )

    violation = inspect_response_language(text, "ko")

    assert violation is not None
    assert violation.language == "ko"
    assert violation.count >= 3
    assert violation.sample


def test_korean_guard_rejects_vietnamese_specific_letters():
    violation = inspect_response_language("출력 문 thư가 잘못됐습니다.", "ko")

    assert violation is not None


def test_korean_guard_rejects_thai_and_extended_latin_corruption():
    assert inspect_response_language("실제รัน 시간이 다릅니다.", "ko") is not None
    assert inspect_response_language("öldü. 더 확인해야 합니다.", "ko") is not None


def test_korean_guard_rejects_short_low_korean_ascii_gibberish():
    violation = inspect_response_language(
        "화 AuthenticationService 용 자컴 3 sec/screens",
        "ko",
    )

    assert violation is not None
    assert "AuthenticationService" in violation.sample


def test_korean_guard_allows_technical_latin_and_korean():
    text = "vLLM 8004와 Qwen 27B는 정상이며 RuntimeError 원인은 구형 URL입니다."

    assert inspect_response_language(text, "ko") is None


def test_korean_guard_allows_short_technical_korean():
    assert inspect_response_language("HTTP 200으로 정상입니다.", "ko") is None
    assert inspect_response_language(
        "`AuthenticationService` 재시작 완료했습니다.", "ko"
    ) is None


def test_korean_guard_ignores_literal_code_and_urls():
    text = (
        "원문은 코드로 보존합니다: `刚才 التقرير`\n"
        "```text\nсерия 状态\n```\n"
        "주소: https://example.com/状态"
    )

    assert inspect_response_language(text, "ko") is None


def test_guard_is_disabled_for_auto_or_unsupported_language():
    assert normalize_response_language("auto") == ""
    assert normalize_response_language("French") == ""
    assert inspect_response_language("刚才", "auto") is None


def test_repair_and_failure_messages_are_clean_korean_contracts():
    repair = build_response_language_repair_prompt("한국어")
    failure = response_language_failure_message("ko-KR")

    assert "Rewrite the entire answer in concise, natural Korean" in repair
    assert "같은 세션에서 계속 처리" in failure
    assert "새 세션" not in failure
    assert inspect_response_language(failure, "ko") is None
