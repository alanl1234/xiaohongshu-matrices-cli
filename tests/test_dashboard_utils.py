from xhs_cli.dashboard.utils import parse_count, split_terms, viral_score


def test_parse_count_handles_chinese_and_latin_suffixes():
    assert parse_count("1.2万") == 12_000
    assert parse_count("3.5k") == 3_500
    assert parse_count("1,234+") == 1_234
    assert parse_count(None) == 0


def test_viral_score_defaults():
    assert viral_score(100, 20, 10, 5) == 175


def test_split_terms_deduplicates_and_removes_hash():
    assert split_terms("#美食,旅行，美食\n探店") == ["美食", "旅行", "探店"]
