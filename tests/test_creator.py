from unittest.mock import MagicMock

from click.testing import CliRunner

from xhs_cli.cli import cli
from xhs_cli.commands.creator import extract_hashtags


def test_extract_hashtags():
    # Normal case
    assert extract_hashtags("This is a #test and another #hashtag") == ["test", "hashtag"]

    # Empty body
    assert extract_hashtags("") == []

    # URL fragment shouldn't match
    assert extract_hashtags("Visit https://example.com#section for more info") == []

    # Consecutive tags without spaces — only the first is preceded by whitespace
    assert extract_hashtags("Mixed #one#two#three") == ["one"]

    # Tags at start of line
    assert extract_hashtags("#start of line") == ["start"]

    # Mix of languages
    assert extract_hashtags("测试 #中文标签 和 #english tag") == ["中文标签", "english"]

    # Trailing hashtag
    assert extract_hashtags("This is #trailing") == ["trailing"]

    # Pure hashtag body
    assert extract_hashtags("#a #b #c") == ["a", "b", "c"]

    # Emoji hashtag
    assert extract_hashtags("Let's go #🎉party") == ["🎉party"]


def _fake_post_client():
    """A mock XhsClient that "resolves" 考研 but fails everything else."""
    client = MagicMock()

    def fake_search(topic: str):
        if topic == "考研":
            return {"topic_info_dtos": [{"id": "65a1b2", "name": "考研", "type": "topic"}]}
        return {"topic_info_dtos": []}

    client.search_topics.side_effect = fake_search
    client.get_upload_permit.return_value = {"fileId": "f1", "token": "t1"}
    client.create_image_note.return_value = {"note_id": "new123"}
    return client


def test_post_resolves_topics_and_skips_unresolved_via_explicit_id(tmp_path, monkeypatch):
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG")
    client = _fake_post_client()
    monkeypatch.setattr(
        "xhs_cli.commands._common.run_client_action", lambda ctx, action: action(client)
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "post",
            "--title", "t",
            "--body", "正文 #考研 #冷门词",
            "--images", str(img),
            "--topic-id", "冷门词=99zz",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    import json

    # The structured JSON is emitted after the (stderr-mirrored) progress lines.
    data = json.loads(result.output[result.output.index("{") :])
    # 冷门词 resolved via explicit id => no unresolved topics
    assert data["data"]["unresolved_topics"] == []
    topics = client.create_image_note.call_args.kwargs["topics"]
    ids = [t["id"] for t in topics]
    assert "65a1b2" in ids and "99zz" in ids


def test_post_warns_on_unresolved_topic(tmp_path, monkeypatch):
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG")
    client = _fake_post_client()
    monkeypatch.setattr(
        "xhs_cli.commands._common.run_client_action", lambda ctx, action: action(client)
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "post",
            "--title", "t",
            "--body", "正文 #完全冷门",
            "--images", str(img),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    import json

    data = json.loads(result.output[result.output.index("{") :])
    # #完全冷门 cannot be resolved and has no explicit id => reported as unresolved
    assert data["data"]["unresolved_topics"] == ["#完全冷门"]
    # The plain-text warning must be surfaced to the user.
    combined = result.output + (result.stderr or "")
    assert "完全冷门" in combined and ("未关联" in combined or "纯文字" in combined)
