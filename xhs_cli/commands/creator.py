"""Creator commands: post, post-text, my-notes, delete."""

import re
from pathlib import Path

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


def _validate_text_note_lengths(title: str, body: str) -> None:
    """Enforce Xiaohongshu's per-field length ceilings for text notes."""
    if len(title) > 20:
        raise click.UsageError(
            f"标题长度 {len(title)} 超过小红书限制 20 字；请精简标题。"
        )
    if len(body) > 1000:
        raise click.UsageError(
            f"正文长度 {len(body)} 超过小红书限制 1000 字；正文只用于生成图片，"
            "若只为发布真实内容，请用 `post --images ...` 重新组织。"
        )


@click.command("post-text")
@click.option("--title", required=True, help="Note title (≤20 chars)")
@click.option(
    "--body",
    required=True,
    help=(
        "Note body in lightweight Markdown. Use ``---`` (alone on a line) "
        "for manual page breaks; otherwise paragraphs are auto-grouped."
    ),
)
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=None,
    help="Read body from a Markdown file (overrides --body).",
)
@click.option(
    "--subtitle",
    default="",
    help="Cover-page subtitle (defaults to first paragraph of body).",
)
@click.option(
    "--theme",
    default="warm",
    show_default=True,
    type=click.Choice(["default", "warm", "playful"]),
    help="Layout theme for cover + content cards.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory for the rendered PNG files (otherwise a temp dir is used).",
)
@click.option("--topic", "topics_flag", multiple=True, help="Topic(s)/hashtag(s) to search and attach")
@click.option(
    "--topic-id",
    "topic_ids_flag",
    multiple=True,
    help="强制话题 id（同 post 命令），KEYWORD=ID。",
)
@click.option(
    "--private/--public",
    "is_private",
    default=True,
    show_default=True,
    help="Publish as private note (DEFAULT). Pure-text publishing "
    "should always be tested privately first.",
)
@click.option(
    "--keep-artifacts/--no-keep-artifacts",
    default=False,
    show_default=True,
    help="Retain rendered PNG files for inspection.",
)
@click.option(
    "--chars-per-page",
    default=600,
    show_default=True,
    type=int,
    help="Approximate character budget per card when auto-splitting.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Render images locally but do NOT upload or publish. Useful for "
        "previewing the look before posting."
    ),
)
@structured_output_options
@click.pass_context
def post_text(
    ctx,
    title: str,
    body: str,
    body_file: str | None,
    subtitle: str,
    theme: str,
    output_dir: str | None,
    topics_flag: tuple[str, ...],
    topic_ids_flag: tuple[str, ...],
    is_private: bool,
    keep_artifacts: bool,
    chars_per_page: int,
    dry_run: bool,
    as_json: bool,
    as_yaml: bool,
):
    """Publish a long-form text note as a typeset multi-image carousel.

    Renders ``title + body`` into a cover page + N content cards via
    Pillow (pixel-level layout), uploads each card through the existing
    image upload pipeline, and submits them as a normal image note through
    ``create_text_note``.

    **Web API cannot publish a truly image-less note.** The result is a
    multi-image ``image_info`` note on the server side, but visually each
    image is a typeset page of text — readers perceive a long-form reading
    carousel as one note.

    \b
    Examples:
        xhs post-text --title "..." --body "..." --private
        xhs post-text --title "..." --body-file article.md --theme playful
        xhs post-text --title "..." --body "..." --dry-run --output-dir ./out
    """
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8")

    _validate_text_note_lengths(title, body)

    if is_private is False and not dry_run:
        # Public publishing is risky for unverified content — confirm intent.
        click.confirm(
            "即将公开发布（非私密）。确认要公开发布吗？"
            "默认建议先私密测试，确认效果后再公开。",
            abort=True,
        )

    if dry_run:
        print_info("Dry-run: rendering only, no upload or publish.")

    resolved_topics: list[dict[str, str]] = []
    unresolved: list[str] = []
    explicit_ids: dict[str, str] = {}

    if not dry_run:
        # Combine explicit --topic flags with hashtags found in the body text.
        body_hashtags = extract_hashtags(body)
        all_topics = list(topics_flag) + body_hashtags
        unique_topics = list(dict.fromkeys(all_topics))
        if len(unique_topics) > 10:
            print_info(f"Found {len(unique_topics)} topics, using first 10")
            unique_topics = unique_topics[:10]

        for raw in topic_ids_flag:
            if "=" in raw:
                key, val = raw.split("=", 1)
                explicit_ids[key.strip()] = val.strip()

    out_dir_path: Path | None = Path(output_dir) if output_dir else None

    def _publish(client):
        if dry_run:
            # Local-only rendering — no client needed.
            from ..text_card_renderer import render_text_note

            if out_dir_path is not None:
                out_dir_path.mkdir(parents=True, exist_ok=True)
            paths = render_text_note(
                title=title, body=body, theme=theme,
                output_dir=out_dir_path,
                subtitle=subtitle,
            )
            print_success(f"Rendered {len(paths)} cards: {out_dir_path or paths[0].parent}")
            return {"dry_run": True, "image_count": len(paths),
                    "output_dir": str(paths[0].parent),
                    "image_paths": [str(p) for p in paths],
                    "theme": theme}

        if unresolved:
            print_warning(
                f"{len(unresolved)} 个话题未能关联到可点击链接，将作为纯文字发布（不可点击）："
                + "、".join(f"#{u}" for u in unresolved)
                + "。可用 --topic-id 关键字=ID 强制关联。"
            )

        result = client.create_text_note(
            title=title,
            body=body,
            topics=resolved_topics or None,
            is_private=is_private,
            theme=theme,
            output_dir=out_dir_path,
            subtitle=subtitle,
            keep_artifacts=keep_artifacts,
        )
        if isinstance(result, dict):
            result.setdefault("unresolved_topics", [f"#{u}" for u in unresolved])
            result.setdefault("image_count", result.get("image_count"))
            result.setdefault("theme", theme)
            result.setdefault("is_private", is_private)
        return result

    if dry_run:
        from ..text_card_renderer import render_text_note
        if out_dir_path is not None:
            out_dir_path.mkdir(parents=True, exist_ok=True)
        paths = render_text_note(
            title=title, body=body, theme=theme,
            output_dir=out_dir_path,
            subtitle=subtitle,
        )
        print_success(
            f"[dry-run] Rendered {len(paths)} cards to "
            f"{out_dir_path or paths[0].parent} ({theme} theme)"
        )
        payload = {
            "dry_run": True,
            "image_count": len(paths),
            "output_dir": str(paths[0].parent),
            "image_paths": [str(p) for p in paths],
            "theme": theme,
            "is_private": is_private,
        }
        if not maybe_print_structured(payload, as_json=as_json, as_yaml=as_yaml):
            pass
        return

    handle_command(
        ctx,
        action=_publish,
        render=lambda _data: print_success(
            f"Text note published: {title}"
            + (f" ({theme} theme, {len(_data.get('image_count', 0))} cards)" if isinstance(_data, dict) else "")
            + (" (private)" if is_private else "")
        ),
        as_json=as_json,
        as_yaml=as_yaml,
    )
