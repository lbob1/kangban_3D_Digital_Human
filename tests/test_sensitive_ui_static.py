"""批7：敏感事件 UI 静态结构断言（不连库、不起服务）。

背景（2026-11-10 用户反馈）：管理端敏感事件表格曾出现操作按钮缺失（非 open 行
只有「—」、无查看入口）与排版错乱（长文本列被压成竖排、时间列折行、操作列贴边）。
本测试用纯文本断言锁住关键结构，防止同类回归：
- admin.html 表格：每行「查看」按钮、wide 宽表类、cell-clip 截断、t-time/ops 列类、
  共同处理徽标、复用 KB.sensitiveDetail；
- sensitive-widget.js：KB.sensitiveDetail 暴露、处理时间线、协助勾选、身份提示、
  补充解决方案弹窗、assist_pending 轮询提醒；
- main.css：wide/cell-clip/t-time/ops 样式存在。
"""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def test_admin_table_has_view_button_for_every_row():
    """操作按钮缺失回归：非 open 行也必须有「查看」入口，不再渲染孤立「—」。"""
    html = _read("admin.html")
    assert "data-view='" in html, "缺少查看按钮 data-view 绑定"
    assert "KB.sensitiveDetail(r, loadSensitiveEvents)" in html, "查看未复用详情弹窗"
    # open 行仍保留标记已处理
    assert "data-resolve='" in html
    # 操作列由 ops 模板统一渲染：每行必有查看按钮，不再出现孤立「—」占位
    segment = html[html.find("function loadSensitiveEvents"):html.find("function resolveSensitive")]
    assert "查看</button>" in segment, "操作列缺少查看按钮"
    assert "\"<td class='ops'>\" + ops" in segment, "操作列未由 ops 模板渲染"


def test_admin_table_layout_classes_present():
    """排版错乱回归：宽表 min-width、长文本截断、时间列与操作列不折行。"""
    html = _read("admin.html")
    assert "table class='kb wide'" in html, "表格未启用 wide 宽表修饰"
    assert "cell-clip" in html, "语句/解决方案列未做截断"
    assert "t-time" in html, "时间列未加不折行类"
    assert "td class='ops'" in html, "操作列未加 nowrap 类"
    css = _read("css/main.css")
    assert "table.kb.wide { min-width" in css
    assert ".cell-clip {" in css
    assert "text-overflow: ellipsis" in css
    assert "td.t-time { white-space: nowrap" in css
    assert "td.ops { white-space: nowrap" in css


def test_admin_table_collab_badge():
    """批7 共同处理徽标：actions 角色≥2 时状态列显示协作标记。"""
    html = _read("admin.html")
    assert "🤝 共同处理" in html


def test_widget_exposes_detail_and_timeline():
    """详情弹窗与时间线：KB.sensitiveDetail 暴露、处理记录时间线、打开即标已读。"""
    js = _read("js/sensitive-widget.js")
    assert "KB.sensitiveDetail = openDetail" in js
    assert "actionsTimeline" in js
    assert "/read" in js  # 打开详情即标已读


def test_widget_resolve_form_identity_and_assist():
    """处理弹窗：身份提示 + 两栏必填 + 「请医生/护士协助」勾选。"""
    js = _read("js/sensitive-widget.js")
    assert "您将以" in js and "身份处理" in js
    assert "id='wo-assist'" in js
    assert "assist_requested" in js
    assert "原因说明与解决方案均为必填项" in js


def test_widget_supplement_flow():
    """批7 补充解决方案：resolved 后可追加处理记录（另一角色完善闭环）。"""
    js = _read("js/sensitive-widget.js")
    assert "/supplement" in js
    assert "补充解决方案" in js
    assert "openSupplementForm" in js


def test_widget_collab_and_assist_indicators():
    """批7 协作标识：共同处理徽标、待我协助提醒、协助中提示。"""
    js = _read("js/sensitive-widget.js")
    assert "collabTag" in js
    assert "assist_pending_for_me" in js
    assert "协助请求提醒" in js
