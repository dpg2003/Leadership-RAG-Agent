import pytest

from leadership.pipeline.resolve import Identity, NameRecord, compare_identity


def rec(name, *functions, company_id=1):
    return NameRecord(company_id, name, frozenset(functions))


def test_vlad_vladimir_merge_with_overlapping_function():
    site = rec("Vlad Tenev", "other")  # company site: "Co-founder & CEO"
    sec = rec("Vladimir Tenev", "other")  # 10-K: "Chief Executive Officer"
    assert compare_identity(site, sec) is Identity.SAME
    assert compare_identity(sec, site) is Identity.SAME


def test_different_people_same_last_name_do_not_merge():
    a = rec("John Smith", "sales")
    b = rec("Mary Smith", "sales")
    assert compare_identity(a, b) is Identity.DIFFERENT


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (rec("Bob Lee", "finance"), rec("Robert Lee", "finance")),  # nickname map
        (rec("Dan Gallagher", "legal"), rec("Daniel Gallagher", "legal")),  # prefix
        (rec("Jason S. Warnick", "finance"), rec("Jason Warnick")),  # identical once normalized
    ],
)
def test_merges(a, b):
    assert compare_identity(a, b) is Identity.SAME


@pytest.mark.parametrize(
    ("a", "b", "why"),
    [
        (rec("Vlad Tenev", "other"), rec("Vladimir Tenev", "engineering"), "no function overlap"),
        (rec("Vlad Tenev", "other"), rec("Vladimir Tenev"), "no functions known"),
        (rec("Al Ng", "data"), rec("Alan Ng", "data"), "prefix too short"),
        (rec("Steven Quirk", "sales"), rec("Stephen Quirke", "sales"), "last-name spelling"),
        (rec("Christine Lee", "sales"), rec("Christopher Lee", "sales"), "same initial only"),
    ],
)
def test_close_but_not_merged_is_flagged(a, b, why):
    assert compare_identity(a, b) is Identity.POSSIBLE_DUPLICATE, why


def test_prefix_rule_known_risk_chris_christine():
    # Accepted risk of the prefix rule (DECISIONS.md D6): "Chris" is a prefix of "Christine",
    # so with an overlapping function they merge. Every non-exact merge is listed in
    # VALIDATION.md, so a wrong one is visible to the human reviewer.
    assert compare_identity(rec("Chris Lee", "sales"), rec("Christine Lee", "sales")) is (
        Identity.SAME
    )


def test_different_company_never_merges():
    assert compare_identity(rec("Vlad Tenev", company_id=1), rec("Vlad Tenev", company_id=2)) is (
        Identity.DIFFERENT
    )
