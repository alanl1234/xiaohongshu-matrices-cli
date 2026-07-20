"""Creator commands: post, my-notes, delete."""

import re

import click

from ..command_normalizers import resolve_topic_payload
from ..formatter import (
    extract_note_id,
    maybe_print_structured,
    print_info,
    print_success,
    print_warning,
    render_creator_notes,
)
from ..note_refs import save_index_from_notes
from ._common import exit_for_error, handle_command, run_client_action, structured_output_options


def extract_hashtags(body: str) -> list[str]:
    """Extract hashtag names from body text.

    Matches '#tag' at start-of-string or preceded by whitespace.
    Does not match URL fragments like 'https://example.com#section'.
    """
    return re.findall(r"(?:^|(?<=\s))#([^\s#]+)", body)


@click.command()
@click.option("--title", required=True, help="Note title")
@click.option("--body", required=True, help="Note body text")
@click.option("--images", required=True, multiple=True, help="Image file path(s)")
@click.option("--topic", "topics_flag", multiple=True, help="Topic(s)/hashtag(s) to search and attach")
@click.option(
    "--topic-id",
    "topic_ids_flag",
    multiple=True,
    help="显式指定话题 id，格式 KEYWORD=ID（如 --topic-id \"考研=65a1b2...\")。"
    "当话题搜索失败/无结果时，强制用该 id 关联，避免话题退化为不可点击的纯文字。",
)
@click.option("--private", "is_private", is_flag=True, help="Publish as private note")
@structured_output_options
@click.pass_context
def post(
    ctx,
    title: str,
    body: str,
    images: tuple[str, ...],
    topics_flag: tuple[str, ...],
    topic_ids_flag: tuple[str, ...],
    is_private: bool,
    as_json: bool,
    as_yaml: bool,
):
    """Publish an image note.

    Topics are auto-linked when found via search; use --topic-id KEYWORD=ID to
    force-link a topic whose search is flaky. Topics that
    cannot be linked degrade to plain text and are NOT clickable — the command
    warns when this happens.
    """

    def _publish(client):
        file_ids = []
        for img_path in images:
            print_info(f"Uploading {img_path}...")
            permit = client.get_upload_permit()
            client.upload_file(permit["fileId"], permit["token"], img_path)
            file_ids.append(permit["fileId"])
            print_success(f"Uploaded: {img_path}")

        # Combine CLI --topic flags with hashtags found in the body text
        body_hashtags = extract_hashtags(body)
        all_topics = list(topics_flag) + body_hashtags
        unique_topics = list(dict.fromkeys(all_topics))  # deduplicate, preserve order

        if len(unique_topics) > 10:
            print_info(f"Found {len(unique_topics)} topics, using first 10")
            unique_topics = unique_topics[:10]

        # Explicit KEYWORD=ID overrides for topics that fail to resolve by name.
        explicit_ids: dict[str, str] = {}
        for raw in topic_ids_flag:
            if "=" in raw:
                key, val = raw.split("=", 1)
                explicit_ids[key.strip()] = val.strip()

        resolved_topics = []
        unresolved: list[str] = []
        for t in unique_topics:
            payload, miss = resolve_topic_payload(
                client, t, explicit_id=explicit_ids.get(t)
            )
            if payload:
                resolved_topics.append(payload)
            if miss:
                unresolved.append(miss)

        if unresolved:
            print_warning(
                f"{len(unresolved)} 个话题未能关联到可点击链接，将作为纯文字发布（不可点击）："
                + "、".join(f"#{u}" for u in unresolved)
                + "。可用 --topic-id 关键字=ID 强制关联。"
            )

        result = client.create_image_note(
            title=title,
            desc=body,
            image_file_ids=file_ids,
            topics=resolved_topics,
            is_private=is_private,
        )
        # Always surface unlinked topics in structured output so downstream
        # checks have a stable field (empty list when everything linked).
        result = dict(result) if isinstance(result, dict) else {"raw": result}
        result["unresolved_topics"] = [f"#{u}" for u in unresolved]
        return result

    handle_command(
        ctx,
        action=_publish,
        render=lambda _data: print_success(f"Note published: {title}" + (" (private)" if is_private else "")),
        as_json=as_json,
        as_yaml=as_yaml,
    )


@click.command("my-notes")
@click.option("--page", default=0, help="Page number (0-indexed)")
@structured_output_options
@click.pass_context
def my_notes(ctx, page: int, as_json: bool, as_yaml: bool):
    """List your own published notes."""

    def _my_notes_action(client):
        data = client.get_creator_note_list(page=page)
        notes = data.get("notes", data.get("note_list", []))
        save_index_from_notes(notes)
        return data

    handle_command(
        ctx,
        action=_my_notes_action,
        render=render_creator_notes,
        as_json=as_json,
        as_yaml=as_yaml,
    )


@click.command("delete")
@click.argument("id_or_url")
@structured_output_options
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.pass_context
def delete(ctx, id_or_url: str, as_json: bool, as_yaml: bool, yes: bool):
    """Delete a PUBLISHED note (experimental; public endpoint is unstable).

    NOTE: CLI 不支持草稿管理 —— 草稿只能到 creator.xiaohongshu.com 草稿箱 UI 删除，
    探测草稿 API 端点均返回 404。本命令仅作用于已发布笔记。
    """
    note_id = extract_note_id(id_or_url)
    if not note_id:
        exit_for_error(ValueError("无法从输入中解析出笔记 ID，请检查参数或链接"), as_json=as_json, as_yaml=as_yaml)

    if not yes:
        click.confirm(f"Delete note {note_id}?", abort=True)

    try:
        data = run_client_action(ctx, lambda client: client.delete_note(note_id))
        if not maybe_print_structured(data, as_json=as_json, as_yaml=as_yaml):
            print_success(f"Deleted note {note_id}")
    except Exception as exc:
        exit_for_error(exc, as_json=as_json, as_yaml=as_yaml)
