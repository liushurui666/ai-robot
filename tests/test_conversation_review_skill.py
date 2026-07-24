from pathlib import Path


def test_weekly_review_skill_uses_requested_fixed_template():
    root = Path(__file__).resolve().parents[1]
    content = (
        root / "config" / "workspace" / "skills" / "conversation-review" / "SKILL.md"
    ).read_text(encoding="utf-8")

    headings = [
        "一、本周主要工作业绩",
        "当前存在问题：",
        "需要支持：",
        "二、下周工作主要开展",
    ]
    positions = [content.index(heading) for heading in headings]

    assert positions == sorted(positions)
    assert "always use this exact section order" in content
    assert "Keep all four weekly-review blocks" in content
    assert "never invent an issue, support request, or next-week plan" in content
