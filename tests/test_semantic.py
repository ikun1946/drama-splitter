"""阶段4 测试：事件索引、请求校验、预算、判定器、整集复核、三种策略。

关键立场（§11.3）：
- 判定结果没有概率字段，只有等级 + 证据 + 风险；
- 降级的表现是"没有判断"，而不是"判断为可用"；
- 模型输出解析失败如实标记，绝不猜测等级。

真实 LLM 推理的验收在 TestRealLLM：模型或推理栈缺失时跳过并给出启用方法。
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from app.core.asr import Transcript, build_sentences
from app.core.candidates import (
    CandidatePoint,
    CandidateSet,
    SOURCE_SENTENCE_END,
    SOURCE_SHOT_CUT,
)
from app.core.review import review_plan
from app.core.semantic import (
    Budget,
    BudgetExhaustedError,
    EventIndex,
    JudgmentRequest,
    LocalLlmJudge,
    ModelUnavailableError,
    NullJudge,
    VERDICT_ACCEPTABLE,
    VERDICT_GOOD,
    VERDICT_POOR,
    VERDICT_UNPARSEABLE,
    build_request,
    judge_candidates,
    parse_verdict,
)
from app.core.solver import SolverWeights, solve_from_candidates
from app.core.timebase import TimeBase

FPS = 25


class FakeMedia:
    """供求解器与 BoundaryPlan 使用的最小媒体对象。"""

    def __init__(self, total_seconds: float = 100.0, fps: int = FPS) -> None:
        self.fps = fps
        self.total_frames = int(total_seconds * fps)
        self.video = type(
            "V", (), {"nominal_fps": Fraction(fps), "nb_frames": self.total_frames}
        )()
        self.video_time_base = TimeBase(num=1, den=12800)

    def duration_ticks(self) -> int:
        return self.total_frames * (12800 // self.fps)

    def frame_start_seconds(self, index: int) -> Fraction:
        return Fraction(index, self.fps)


@pytest.fixture
def media():
    return FakeMedia(100.0)


def word(start: float, end: float, text: str):
    from app.core.asr import AsrWord

    return AsrWord(start=Fraction(str(start)), end=Fraction(str(end)), text=text)


def derived(target: float, low: float, high: float, total: float = 100.0):
    from app.core.settings import DerivedParams

    return DerivedParams(
        target_duration=Fraction(str(target)),
        min_duration=Fraction(str(low)),
        max_duration=Fraction(str(high)),
        total_seconds=Fraction(str(total)),
    )


def make_transcript(lines: list[tuple[float, float, str]]) -> Transcript:
    transcript = Transcript(duration=Fraction(100))
    transcript.words = [word(s, e, t) for s, e, t in lines]
    transcript.sentences = build_sentences(transcript.words)
    return transcript


# ---------------------------------------------------------------------------
# 事件索引
# ---------------------------------------------------------------------------


class TestEventIndex:
    def test_sentence_queries(self):
        transcript = make_transcript(
            [(1.0, 3.0, "第一句。"), (5.0, 7.0, "第二句？"), (9.0, 11.0, "第三句。")]
        )
        index = EventIndex.build(transcript)
        assert len(index.dialogue) == 3
        # t=4.5：第一句已结束(3s)、第二句未开始(5s)
        assert index.sentence_before(Fraction(9, 2)).text == "第一句。"
        assert index.sentence_after(Fraction(9, 2)).text == "第二句？"
        # t=6：第二句正在进行（5-7s），before 是第一句，after 是第三句
        assert index.sentence_before(Fraction(6)).text == "第一句。"
        assert index.sentence_after(Fraction(6)).text == "第三句。"

    def test_detects_sentence_cut_in_half(self):
        transcript = make_transcript([(1.0, 5.0, "一句很长的话还没说完。")])
        index = EventIndex.build(transcript)
        covering = index.sentence_covering(Fraction(3))
        assert covering is not None, "3s 处应正在截断句子"

    def test_empty_transcript_is_safe(self):
        index = EventIndex.build(None)
        assert index.is_empty()
        assert index.sentence_before(Fraction(5)) is None
        assert index.sentence_covering(Fraction(5)) is None


# ---------------------------------------------------------------------------
# 请求校验（§ 请求校验）
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def valid_request(self, **overrides) -> JudgmentRequest:
        defaults = dict(
            candidate_time=Fraction(5),
            candidate_frame=125,
            text_before="前面的台词。",
            text_after="后面的台词。",
            gap_after=Fraction(1),
            ends_with_question=False,
            near_shot_cut=False,
            rule_score=0.6,
            strategy="story",
        )
        defaults.update(overrides)
        return JudgmentRequest(**defaults)

    def test_valid_request_has_no_problems(self):
        assert self.valid_request().validate() == []

    def test_negative_time_rejected(self):
        problems = self.valid_request(candidate_time=Fraction(-1)).validate()
        assert any("candidate_time" in p for p in problems)

    def test_score_out_of_range_rejected(self):
        problems = self.valid_request(rule_score=1.5).validate()
        assert any("rule_score" in p for p in problems)

    def test_empty_context_rejected(self):
        """前后台词都为空时没有语义证据，判断是在猜——直接拒绝。"""
        problems = self.valid_request(text_before="", text_after="").validate()
        assert any("前后台词均为空" in p for p in problems)


# ---------------------------------------------------------------------------
# 预算（§10.4 BUDGET_EXHAUSTED）
# ---------------------------------------------------------------------------


class TestBudget:
    def test_consume_until_exhausted(self):
        budget = Budget(max_requests=3)
        for _ in range(3):
            budget.try_consume()
        with pytest.raises(BudgetExhaustedError):
            budget.try_consume()
        assert budget.remaining() == 0

    def test_describe(self):
        budget = Budget(max_requests=5)
        budget.try_consume()
        assert "1/5" in budget.describe()


# ---------------------------------------------------------------------------
# 判定器：解析严格性（不依赖模型）
# ---------------------------------------------------------------------------


class TestVerdictParsing:
    def test_parses_valid_json(self):
        verdict = parse_verdict(
            '{"verdict": "good_cut", "evidence": ["句号收尾"], "risks": []}',
            judge_id="test",
        )
        assert verdict.verdict == VERDICT_GOOD
        assert verdict.evidence == ["句号收尾"]
        assert verdict.judge_id == "test"

    def test_tolerates_surrounding_text(self):
        verdict = parse_verdict(
            '好的，我的判断如下：{"verdict": "poor_cut", "evidence": ["截断台词"], "risks": ["x"]} 以上',
            judge_id="test",
        )
        assert verdict.verdict == VERDICT_POOR

    def test_no_json_is_reported_not_guessed(self):
        verdict = parse_verdict("我觉得这里不错", judge_id="test")
        assert verdict.verdict == VERDICT_UNPARSEABLE
        assert any("没有 JSON" in r for r in verdict.risks)

    def test_invalid_verdict_value_is_reported(self):
        verdict = parse_verdict('{"verdict": "非常好", "evidence": []}', judge_id="test")
        assert verdict.verdict == VERDICT_UNPARSEABLE
        assert any("verdict 非法" in r for r in verdict.risks)

    def test_missing_evidence_adds_low_confidence_risk(self):
        verdict = parse_verdict('{"verdict": "acceptable"}', judge_id="test")
        assert any("缺少证据" in r for r in verdict.risks)


class TestNullJudge:
    def test_degradation_means_no_judgment(self):
        """降级的表现是"没有判断"，而不是"判断为可用"。"""
        judge = NullJudge()
        assert not judge.available()
        with pytest.raises(ModelUnavailableError):
            judge.judge(
                JudgmentRequest(
                    candidate_time=Fraction(1), candidate_frame=25,
                    text_before="a", text_after="b", gap_after=None,
                    ends_with_question=False, near_shot_cut=False,
                    rule_score=0.5, strategy="story",
                )
            )


class TestLocalLlmJudgeOffline:
    """不加载模型的判定器行为（模型文件缺失时也应行为正确）。"""

    def test_missing_model_raises_unavailable(self, tmp_path):
        with pytest.raises(ModelUnavailableError):
            LocalLlmJudge(tmp_path / "nope.gguf")

    def test_missing_stack_raises_unavailable(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "llama_cpp":
                raise ImportError("模拟推理栈缺失")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        model = tmp_path / "m.gguf"
        model.write_bytes(b"x" * 2048)
        with pytest.raises(ModelUnavailableError):
            LocalLlmJudge(model)

    def test_request_validation_short_circuits_inference(self, tmp_path, monkeypatch):
        """请求不合法时不应调用推理——直接返回未解析标记。"""
        calls = {"count": 0}

        class FakeLlama:
            def __init__(self, *args, **kwargs):
                pass

            def create_completion(self, *args, **kwargs):
                calls["count"] += 1
                return {"choices": [{"text": '{"verdict": "good_cut", "evidence": ["x"]}'}]}

        import app.core.semantic as sem

        model = tmp_path / "m.gguf"
        model.write_bytes(b"x" * 2048)
        monkeypatch.setattr(sem, "_import_llama", lambda: FakeLlama)

        judge = LocalLlmJudge(model)
        bad = JudgmentRequest(
            candidate_time=Fraction(-1), candidate_frame=-1,
            text_before="a", text_after="b", gap_after=None,
            ends_with_question=False, near_shot_cut=False,
            rule_score=0.5, strategy="story",
        )
        verdict = judge.judge(bad)
        assert calls["count"] == 0, "请求不合法不应触发推理"
        assert verdict.verdict == VERDICT_UNPARSEABLE


# ---------------------------------------------------------------------------
# 判断执行：预算与降级
# ---------------------------------------------------------------------------


class TestJudgeCandidates:
    def build(self, count: int = 6) -> tuple[CandidateSet, EventIndex]:
        transcript = make_transcript(
            [(float(i * 5), float(i * 5 + 3), f"第{i}句。") for i in range(1, count + 2)]
        )
        index = EventIndex.build(transcript)
        candidates = CandidateSet(
            points=[
                CandidatePoint(
                    time=Fraction(str(4.0 + i * 5.0)),
                    frame_index=int((4.0 + i * 5.0) * 25),
                    sources=[SOURCE_SENTENCE_END],
                    score=0.5 + i * 0.05,
                    speech_before=f"第{i}句。",
                    speech_after=f"第{i + 1}句。",
                )
                for i in range(count)
            ]
        )
        return candidates, index

    def test_degraded_mode_leaves_everything_pending(self):
        """降级 = 没有判断：分数不变、状态保持 pending、留下记录。"""
        candidates, index = self.build()
        before_scores = [p.score for p in candidates.points]
        outcome = judge_candidates(candidates, index, None, strategy="story")

        assert outcome.degraded
        assert outcome.judged == 0
        assert [p.score for p in candidates.points] == before_scores
        assert all(p.review_status == "pending" for p in candidates.points)
        assert outcome.notes, "降级必须留下说明"

    def test_null_judge_degrades_identically(self):
        candidates, index = self.build()
        outcome = judge_candidates(candidates, index, NullJudge(), strategy="story")
        assert outcome.degraded

    def test_budget_limits_judgments_and_is_recorded(self):
        """§8.4/§10.4：预算用尽后停止并记录跳过数量。"""
        candidates, index = self.build(count=8)

        class CountingJudge:
            def __init__(self):
                self.calls = 0

            def available(self):
                return True

            def describe(self):
                return "计数器"

            def judge(self, request):
                self.calls += 1
                from app.core.semantic import JudgmentVerdict

                return JudgmentVerdict(verdict=VERDICT_ACCEPTABLE, judge_id="counter")

        judge = CountingJudge()
        outcome = judge_candidates(
            candidates, index, judge, strategy="story", budget_requests=3
        )
        assert outcome.judged == 3
        assert judge.calls == 3
        assert outcome.skipped_by_budget == 5
        assert any("预算" in note for note in outcome.notes)

    def test_semantic_verdict_adjusts_ranking(self):
        """判定结果应影响排序：poor_cut 的候选被降权。"""
        candidates, index = self.build(count=2)
        candidates.points[0].score = 0.6
        candidates.points[1].score = 0.6
        candidates.points[0].speech_before = "前一句。"
        candidates.points[1].speech_before = "前一句。"

        class FixedJudge:
            """第一个候选判 poor，第二个判 good——分数应拉开。"""

            def __init__(self):
                self.index = 0

            def available(self):
                return True

            def describe(self):
                return "固定判定"

            def judge(self, request):
                from app.core.semantic import JudgmentVerdict

                self.index += 1
                verdict = VERDICT_POOR if self.index == 1 else VERDICT_GOOD
                return JudgmentVerdict(verdict=verdict, evidence=["固定"], judge_id="fixed")

        outcome = judge_candidates(candidates, index, FixedJudge(), strategy="story")
        assert outcome.judged == 2
        scores = [p.score for p in sorted(candidates.points, key=lambda p: -p.score)]
        assert scores[0] > scores[1], f"判定未影响排序：{scores}"
        # good: 0.6 + 0.20 = 0.80；poor: 0.6 - 0.30 = 0.30
        assert scores[0] == pytest.approx(0.80, abs=1e-9)
        assert scores[1] == pytest.approx(0.30, abs=1e-9)
        assert all(p.review_status == "semantic_reviewed" for p in candidates.points)


# ---------------------------------------------------------------------------
# 整集复核
# ---------------------------------------------------------------------------


class TestEpisodeReview:
    def build_plan(self, cuts: list[float], total: float = 40.0) -> "BoundaryPlan":
        from app.core.plan import BoundaryPlan

        time_base = TimeBase(num=1, den=12800)
        return BoundaryPlan.from_interior_cuts(
            [Fraction(str(c)) for c in cuts], time_base, int(total * 12800)
        )

    def test_clean_episode_passes(self):
        transcript = make_transcript([(1.0, 4.0, "第一句。"), (6.0, 9.0, "第二句。")])
        index = EventIndex.build(transcript)
        plan = self.build_plan([5.0])  # 一集 0-5，二集 5-40
        reviews = review_plan(plan, None, index)
        assert reviews[0].grade == "pass"
        assert reviews[0].risks == []
        assert any("完整对白" in e for e in reviews[0].evidence)

    def test_cut_inside_sentence_is_blocking(self):
        """切在句中是最严重的风险——必须阻断，不能只提醒。"""
        transcript = make_transcript([(1.0, 9.0, "一句很长的话还没说完。")])
        index = EventIndex.build(transcript)
        plan = self.build_plan([5.0])  # 5s 落在句子中间
        reviews = review_plan(plan, None, index)
        assert any(r.grade == "block" for r in reviews)
        assert any("截断了句子" in risk for risk in reviews[0].risks)

    def test_episode_without_dialogue_warns(self):
        transcript = make_transcript([(30.0, 33.0, "远处的一句话。")])
        index = EventIndex.build(transcript)
        plan = self.build_plan([5.0])  # 第一集 0-5 没有对白
        reviews = review_plan(plan, None, index)
        assert any("没有完整对白" in risk for risk in reviews[0].risks)
        assert reviews[0].grade == "warn"

    def test_json_roundtrip(self):
        transcript = make_transcript([(1.0, 4.0, "第一句。"), (6.0, 9.0, "第二句。")])
        index = EventIndex.build(transcript)
        plan = self.build_plan([5.0])
        reviews = review_plan(plan, None, index)
        payload = json.dumps([r.to_json() for r in reviews], ensure_ascii=False)
        assert "pass" in payload


# ---------------------------------------------------------------------------
# 三种策略（§ 策略）
# ---------------------------------------------------------------------------


class TestStrategies:
    def test_weights_differ_by_strategy(self):
        story = SolverWeights.for_strategy("story")
        suspense = SolverWeights.for_strategy("suspense")
        duration = SolverWeights.for_strategy("duration")
        assert suspense.boundary > story.boundary > duration.boundary
        assert duration.duration > story.duration > suspense.duration

    def test_unknown_strategy_falls_back(self):
        weights = SolverWeights.for_strategy("不存在的策略")
        assert weights.duration == 0.7 and weights.boundary == 0.3

    def test_suspense_bonus_requires_question_flag(self):
        from app.core.solver import strategy_boundary_bonus

        bonus = strategy_boundary_bonus(
            "suspense", {250: 0.5, 500: 0.5}, {250: True, 500: False}
        )
        assert bonus == {250: -0.15}

    def test_suspense_bonus_ignored_for_other_strategies(self):
        from app.core.solver import strategy_boundary_bonus

        assert strategy_boundary_bonus("story", {250: 0.5}, {250: True}) == {}
        assert strategy_boundary_bonus("duration", {250: 0.5}, {250: True}) == {}

    def test_duration_strategy_changes_boundary_choice(self, media):
        """时长优先时，边界质量让位于集长贴合度。

        数据设计：22s 处候选分 0.9（贴合度差 3s），27s 处候选分 0.1（差 2s）。
        - 剧情优先（0.6/0.4）：质量分主导 → 选 22s；
        - 时长优先（0.85/0.15）：时长收益 0.16 超过质量损失 0.8×0.15=0.12 → 选 27s。
        两种策略给出**不同的**切点，且各自全部合法。
        """
        candidates = CandidateSet(
            points=[
                CandidatePoint(
                    time=Fraction(22), frame_index=550,
                    sources=[SOURCE_SENTENCE_END], score=0.9,
                ),
                CandidatePoint(
                    time=Fraction(27), frame_index=675,
                    sources=[SOURCE_SHOT_CUT], score=0.1,
                ),
                CandidatePoint(
                    time=Fraction(51), frame_index=1275,
                    sources=[SOURCE_SENTENCE_END], score=0.5,
                ),
                CandidatePoint(
                    time=Fraction(74), frame_index=1850,
                    sources=[SOURCE_SENTENCE_END], score=0.5,
                ),
            ]
        )
        common = dict(
            settings_count_exact=True, target_episode_count=4,
            allowed_min=4, allowed_max=4,
        )
        story_plan, story_solution, _, _ = solve_from_candidates(
            candidates, media, derived(25, 20, 30), strategy="story", **common
        )
        duration_plan, duration_solution, _, _ = solve_from_candidates(
            candidates, media, derived(25, 20, 30), strategy="duration", **common
        )
        # 两种策略都必须产出合法方案
        for solution in (story_solution, duration_solution):
            assert solution.episode_count == 4
            assert all(Fraction(20) <= d <= Fraction(30) for d in solution.durations)
        assert Fraction(22) in story_solution.cut_times, (
            f"剧情优先应选 22s：{[str(t) for t in story_solution.cut_times]}"
        )
        assert Fraction(27) in duration_solution.cut_times, (
            f"时长优先应选 27s：{[str(t) for t in duration_solution.cut_times]}"
        )


# ---------------------------------------------------------------------------
# 真实 LLM 推理（模型与推理栈就绪时才算数）
# ---------------------------------------------------------------------------


class TestRealLLM:
    @pytest.fixture(scope="class")
    def judge(self):
        from app.core.semantic import locate_llm_model

        model = locate_llm_model()
        if model is None:
            pytest.skip("未下载 LLM 模型。启用命令：python tools/fetch_llm.py")
        try:
            from app.core.semantic import LocalLlmJudge

            return LocalLlmJudge.from_model_dir(model.parent, n_ctx=1024)
        except ModelUnavailableError as exc:
            pytest.skip(f"推理栈不可用：{exc}")

    def test_probe_returns_valid_verdict(self, judge):
        """真实推理冒烟：输出必须是可解析的合法等级。"""
        verdict_text = judge.probe()
        assert any(v in verdict_text for v in (VERDICT_GOOD, VERDICT_ACCEPTABLE, VERDICT_POOR))

    def test_judges_a_real_cut_context(self, judge):
        from app.core.semantic import JudgmentRequest

        request = JudgmentRequest(
            candidate_time=Fraction(4),
            candidate_frame=100,
            text_before="你觉得我会信吗？",
            text_after="信不信由你，反正我问心无愧。",
            gap_after=Fraction(6, 10),
            ends_with_question=True,
            near_shot_cut=False,
            rule_score=0.7,
            strategy="suspense",
        )
        verdict = judge.judge(request)
        assert verdict.verdict in {VERDICT_GOOD, VERDICT_ACCEPTABLE, VERDICT_POOR}, (
            f"模型输出无法解析：{verdict.raw_output[:200]}"
        )
        assert verdict.evidence, "真实判断应给出证据"
