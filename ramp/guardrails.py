"""红线拦截：五条硬禁止，拦在模型调用之前。

为什么是规则而不是让模型自己判断——三个理由，面试会问：
  1. **成本**：命中红线的请求不产生任何 token 消耗（¥0.00）。
  2. **不可绕过**：模型的自我约束可以被 prompt 注入撬开，前置规则不能。
  3. **可审计**：每次拦截都能指到具体哪一条规则，而不是"模型觉得不该答"。

代价是召回不完美——规则会漏掉换了说法的越界提问。所以这一层是
**下限而非上限**：漏网的由系统提示词里的行为约束兜第二道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    patterns: tuple[re.Pattern[str], ...]
    reply: str
    referral: str


def _p(*raw: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(r, re.IGNORECASE) for r in raw)


RULES: tuple[Rule, ...] = (
    Rule(
        id="H3-01",
        name="薪酬对比与他人薪酬",
        patterns=_p(
            r"(他|她|别人|同事|隔壁|其他人|谁).{0,12}(工资|薪资|薪水|年薪|月薪|收入|多少钱)",
            r"(工资|薪资|薪水|年薪|月薪).{0,10}(排名|水平|对比|比较|高低|多少)",
            r"我.{0,6}(工资|薪资).{0,10}(组里|团队|同级|同事).{0,8}(什么水平|高吗|低吗)",
            r"[一-鿿]{2,4}(的)?(工资|薪资|年薪|月薪)是?多少",
        ),
        reply=(
            "这个我不能回答。薪酬信息属于个人隐私，我不提供任何形式的查询或对比。\n\n"
            "如果你想了解**自己**的薪酬构成，可以找你所在部门的 HRBP。"
        ),
        referral="HRBP",
    ),
    Rule(
        id="H3-02",
        name="他人绩效、职级与未公开组织调整",
        patterns=_p(
            r"(他|她|别人|同事|谁).{0,12}(绩效|考核结果|评级|打了什么|拿了什么档|职级|level|P\d)",
            r"(裁员|优化|架构调整|组织调整|要合并|要拆分|谁要走|谁被裁)",
            r"(谁|哪些人).{0,8}(要离职|被辞退|要被优化)",
        ),
        reply=(
            "这个我不能回答。他人的绩效、职级以及尚未公开的组织信息，我不提供。\n\n"
            "如果是你自己的绩效结果或职级问题，可以找你所在部门的 HRBP。"
        ),
        referral="HRBP",
    ),
    Rule(
        id="H3-03",
        name="劳动纠纷、投诉与举报",
        patterns=_p(
            r"(告|起诉|仲裁|劳动仲裁|劳动监察|申诉|维权).{0,10}(公司|老板|领导|hr)",
            r"(投诉|举报).{0,10}(领导|同事|上级|老板|公司)",
            r"(违法|不合法|违反劳动法|赔偿金|n\+1|2n)",
            r"(被辞退|被开除|强制加班|不给加班费).{0,12}(怎么办|怎么维权|能不能)",
        ),
        reply=(
            "这类问题我不适合给建议，也不会留存这段对话。\n\n"
            "请直接联系你所在部门的 HRBP；如果你希望绕开直属线，"
            "公司通常另设独立于业务汇报线的合规举报渠道，具体入口请向 HRBP 索取。"
        ),
        referral="HRBP / 合规渠道",
    ),
    Rule(
        id="H3-04",
        name="代签署文件",
        patterns=_p(
            r"(帮我|替我|代我).{0,8}(签|签署|签字|确认签)",
            r"(签|签署).{0,6}(劳动合同|保密协议|承诺书|竞业|协议)",
            r"(帮我|替我).{0,8}(点|勾选|同意).{0,6}(条款|协议|承诺)",
        ),
        reply=(
            "签署类操作我不能代做——合同、保密协议、承诺书都需要你本人确认。\n\n"
            "我可以帮你**找到入口并解释条款**：电子签在「云启 HR」→ 我的待办。需要我说明某一条吗？"
        ),
        referral="云启 HR 电子签",
    ),
    Rule(
        id="H3-05",
        name="对个人的评价性结论",
        patterns=_p(
            r"(评价|点评|打分|评估).{0,8}(一下)?.{0,6}(我|他|她|这个新人|某某)",
            r"(我|他|她).{0,8}(学习能力|工作能力|表现|靠不靠谱|行不行|适不适合).{0,8}(怎么样|如何|吗)",
            r"(谁|哪个人).{0,8}(更好|更强|更适合|能力强)",
        ),
        reply=(
            "我不对人做评价性判断——这是产品设计上的硬边界。\n\n"
            "我可以给你**事实与信号**：比如你这周在哪些主题上反复查询、哪些节点还没完成。"
            "要看看吗？"
        ),
        referral="—",
    ),
)


@dataclass
class Verdict:
    blocked: bool
    rule_id: str | None = None
    rule_name: str | None = None
    reply: str | None = None
    referral: str | None = None
    persist_memory: bool = True

    @property
    def route(self) -> str:
        return "blocked" if self.blocked else "pass"


PASS = Verdict(blocked=False)


def check(text: str) -> Verdict:
    """在任何模型调用之前跑。命中即终止，成本为零。"""
    t = (text or "").strip()
    if not t:
        return PASS
    for rule in RULES:
        for pat in rule.patterns:
            if pat.search(t):
                return Verdict(
                    blocked=True,
                    rule_id=rule.id,
                    rule_name=rule.name,
                    reply=rule.reply,
                    referral=rule.referral,
                    # H3-03 劳动纠纷：明确不写入任何记忆
                    persist_memory=rule.id != "H3-03",
                )
    return PASS


def rule_table() -> list[dict[str, str]]:
    """给文档 / 运营后台用的规则清单。"""
    return [{"id": r.id, "name": r.name, "referral": r.referral} for r in RULES]
