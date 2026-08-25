"""Served Slice 8 onboarding structure regressions."""

from pathlib import Path


HTML = Path(__file__).resolve().parents[1] / "templates" / "index.html"


def _onboarding_markup() -> str:
    source = HTML.read_text(encoding="utf-8")
    return source[source.index('<dialog class="onboarding-dialog"'):source.index('<!-- FLASH TOAST CONTAINER -->')]


def test_required_financial_inputs_are_all_on_first_onboarding_page() -> None:
    markup = _onboarding_markup()
    page_one = markup[markup.index('data-step="0"'):markup.index('data-step="1"')]
    for field in (
        'onboardingBalance', 'onboardingPayPeriod', 'onboardingNextPayday',
        'onboardingExpectedPaycheck', 'onboardingPyfTarget', 'onboardingSafeBuffer',
    ):
        assert 'id="' + field + '"' in page_one
    assert page_one.count('(Required)') >= 7
    assert 'onboardingExpenses' in page_one


def test_optional_and_location_pages_do_not_hide_required_fields_or_store_selection() -> None:
    markup = _onboarding_markup()
    later_pages = markup[markup.index('data-step="1"'):]
    assert '(Required)' not in later_pages
    assert 'selected_shopping_store' not in markup
    assert 'Choose Store' not in markup
    assert 'Find Stores' not in markup
    assert 'Notification' not in markup


def test_served_controller_uses_slice8a_state_and_retires_preset_bill_templates() -> None:
    source = HTML.read_text(encoding="utf-8")
    onboarding = source[source.index('function initOnboarding()'):source.index('/* ================================================================\n       RECIPES SEARCH')]
    assert '/api/onboarding/required-expenses-review' in onboarding
    assert 'has_expenses_reviewed' in onboarding
    assert "billRow('Phone'" not in onboarding
    assert "billRow('Internet'" not in onboarding
    assert "billRow('Utilities'" not in onboarding
    assert 'locationTouched' in onboarding

