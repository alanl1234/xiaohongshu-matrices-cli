"""analyze-user command: fetch all posts of a Xiaohongshu account and produce a layered analysis."""

import time

import click

from ..formatter import maybe_print_structured, print_info
from ..user_analyzer import (
    build_layered_report,
    build_synthesis,
    extract_record,
    fetch_all_user_notes,
)
from ._common import exit_for_error, run_client_action

AI_INSTRUCTIONS = """
你是小红书内容研究助手。给定一份账号分层分析数据（来自其全部公开笔记的聚合指标），
用中文输出该账号的战略总结。聚焦可复用的内容规律，服务于"二创/风格复刻"研究，
不虚构数据、不编造授权或效果承诺。严格按 schema 返回。
""".strip()


@click.command("analyze-user")
@click.argument("user_id")
@click.option("--deep", is_flag=True, help="逐篇获取笔记详情，补全收藏/评论/话题/正文（更慢，但 L1–L3 更准）")
@click.option("--max-pages", default=None, type=int, help="限制抓取页数（每页约 30 篇），默认抓全部")
@click.option("--limit", default=0, type=int, help="deep 模式下最多取详情的篇数（0=全部）")
@click.option("--delay", default=0.8, type=float, help="deep 模式逐篇请求间隔（秒），避免触发风控")
@click.option("--top", default=10, type=int, help="头部帖子展示数量")
@click.option("--ai", is_flag=True, help="用 AI 生成战略总结（需配置 OPENAI_API_KEY）")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出")
@click.option("--yaml", "as_yaml", is_flag=True, help="以 YAML 输出")
@click.pass_context
def analyze_user(
    ctx,
    user_id: str,
    deep: bool,
    max_pages: int | None,
    limit: int,
    delay: float,
    top: int,
    ai: bool,
    as_json: bool,
    as_yaml: bool,
):
    """分层式分析某个小红书账号的所有帖子。

    USER_ID 为小红书用户 ID（公开作者 id，例如 95653634553），不是本地 dashboard 账号。

    默认：仅用 user_posted 列表接口即可完成全量分层分析（含点赞、形式、主题、节奏）。
    加 --deep：逐篇补详情，补全收藏/评论/正文后重新分层（更耗时）。
    加 --ai：用大模型生成战略总结（需 OPENAI_API_KEY）。

    示例：
        xhs --account 4 analyze-user 95653634553
        xhs --account 4 analyze-user 95653634553 --deep --ai
    """

    def _action(client):
        print_info(f"正在拉取 @{user_id} 的全部帖子…")
        raw = fetch_all_user_notes(client, user_id, max_pages=max_pages, delay=0.0)
        if not raw:
            return None
        try:
            user_info = client.get_user_info(user_id)
        except Exception:
            user_info = {}

        records = [extract_record(n) for n in raw]

        if deep:
            print_info(f"deep 模式：逐篇补全 {len(records)} 篇详情（间隔 {delay}s）…")
            lim = limit if limit else len(records)
            for i, n in enumerate(raw[:lim]):
                nid = n.get("note_id") or n.get("id")
                token = n.get("xsec_token", "")
                try:
                    detail = client.get_note_detail(nid, xsec_token=token, xsec_source="pc_profile")
                    records[i] = extract_record(n, detail=detail)
                except Exception:
                    pass
                if delay:
                    time.sleep(delay)

        report = build_layered_report(records, user_info, top_n=top, deep=deep, user_id=user_id)

        if ai:
            try:
                from ..dashboard.ai import AccountAnalysisReport, OpenAIResponsesProvider

                provider = OpenAIResponsesProvider()
                if provider.configured:
                    synth = provider.generate(
                        AI_INSTRUCTIONS,
                        {"report": report},
                        AccountAnalysisReport,
                        complex_task=True,
                    )
                    report["layer5_synthesis"] = synth.summary
                    report["synthesis_source"] = "ai"
                else:
                    report["synthesis_source"] = "rule"
                    report["layer5_synthesis"] = build_synthesis(report)
            except Exception as exc:  # noqa: BLE001 — AI is best-effort
                report["synthesis_source"] = "rule_error"
                report["layer5_synthesis"] = build_synthesis(report) + f"\n（AI 总结失败：{exc}）"
        else:
            report["synthesis_source"] = "rule"
            report["layer5_synthesis"] = build_synthesis(report)

        return report

    try:
        report = run_client_action(ctx, _action)
    except Exception as exc:  # noqa: BLE001
        exit_for_error(exc, as_json=as_json, as_yaml=as_yaml)
        return

    if report is None:
        print_info(f"未获取到 @{user_id} 的任何帖子，请确认 user_id 是否正确、账号是否已登录。")
        return

    if not maybe_print_structured(report, as_json=as_json, as_yaml=as_yaml):
        from ..formatter import render_user_analysis

        render_user_analysis(report)
