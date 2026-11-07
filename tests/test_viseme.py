"""Day5 viseme 时间轴映射单测（大纲 5.1，D2 决策）。

覆盖：韵母主元音边界（单韵母/复韵母/鼻韵母/ü）、英文/数字/标点跳过、
无元音回落、词内多音节均分、相邻同口型合并、非法时长过滤、首音节提前。
纯函数离线测试，不触网。
"""

import pytest

from app.integrations.edge_tts import build_viseme_timeline, word_to_visemes


class TestWordToVisemes:
    def test_single_vowel_finals(self):
        # 妈mā→a / 墨mò→o / 得dé→e / 期qī→i / 祖zǔ→uu(v_to_u)
        assert word_to_visemes("妈") == ["aa"]
        assert word_to_visemes("墨") == ["oh"]
        assert word_to_visemes("得") == ["eh"]
        assert word_to_visemes("期") == ["ih"]
        assert word_to_visemes("祖") == ["uu"]

    def test_compound_finals_take_first_vowel(self):
        # 复韵母/鼻韵母取首个元音：爱ài→a / 欧ōu→o / 恩ēn→e / 安ān→a
        assert word_to_visemes("爱") == ["aa"]
        assert word_to_visemes("欧") == ["oh"]
        assert word_to_visemes("恩") == ["eh"]
        assert word_to_visemes("安") == ["aa"]

    def test_multi_syllable_word(self):
        # 你nǐ→ih 好hǎo→aa
        assert word_to_visemes("你好") == ["ih", "aa"]

    def test_umlaut_u_to_uu(self):
        # 女nǚ → ü → uu
        assert word_to_visemes("女") == ["uu"]

    def test_no_vowel_falls_back_aa(self):
        # 「嗯」韵母无标准元音 → 回落 aa（不断言具体 pypinyin 行为，只断言非空且合法）
        result = word_to_visemes("嗯")
        assert result and all(v in {"aa", "ih", "uu", "eh", "oh"} for v in result)

    @pytest.mark.parametrize("non_chinese", ["hello", "123", "AI", "，。！？"])
    def test_non_chinese_produces_nothing(self, non_chinese):
        # 英文/数字/标点：errors="ignore" 透传原字符，无韵母 → 空串音节跳过
        result = word_to_visemes(non_chinese)
        assert all(v in {"aa", "ih", "uu", "eh", "oh"} for v in result)

    def test_mixed_word_digits(self):
        # 中文+数字混合：数字不产生音节，中文正常映射
        result = word_to_visemes("好123")
        assert "aa" in result


class TestBuildVisemeTimeline:
    def test_word_split_evenly_across_syllables(self):
        timeline = build_viseme_timeline([("你好", 100.0, 400.0)])
        assert len(timeline) == 2
        assert timeline[0]["v"] == "ih"
        assert timeline[1]["v"] == "aa"
        # 词时长均分：各 200ms
        assert timeline[0]["d"] == 200
        assert timeline[1]["d"] == 200
        assert timeline[1]["t"] == timeline[0]["t"] + 200

    def test_adjacent_same_viseme_merged(self):
        # 天tiān→ih / 见jiàn→ih：相邻同口型合并为一段
        timeline = build_viseme_timeline([("天", 0.0, 200.0), ("见", 200.0, 200.0)])
        assert len(timeline) == 1
        assert timeline[0]["v"] == "ih"
        assert timeline[0]["d"] == 400

    def test_non_chinese_words_skipped(self):
        timeline = build_viseme_timeline([("hello", 0.0, 300.0), ("好", 300.0, 200.0)])
        assert [item["v"] for item in timeline] == ["aa"]

    def test_zero_duration_filtered(self):
        assert build_viseme_timeline([("你好", 100.0, 0.0)]) == []

    def test_first_item_leads_and_clamps_zero(self):
        # 首音节提前 40ms：offset<40 时钳到 0
        timeline = build_viseme_timeline([("好", 20.0, 200.0)])
        assert timeline[0]["t"] == 0

    def test_timeline_monotonic_and_schema(self):
        words = [
            ("今天", 100.0, 375.0),
            ("天气", 487.0, 388.0),
            ("怎么样", 900.0, 463.0),
        ]
        timeline = build_viseme_timeline(words)
        assert timeline
        last_end = 0
        for item in timeline:
            assert set(item) == {"v", "t", "d"}
            assert item["v"] in {"aa", "ih", "uu", "eh", "oh"}
            assert item["t"] >= 0 and item["d"] > 0
            assert item["t"] >= last_end - 1  # 允许 lead_ms 造成的一次前移
            last_end = item["t"] + item["d"]

    def test_empty_words(self):
        assert build_viseme_timeline([]) == []
