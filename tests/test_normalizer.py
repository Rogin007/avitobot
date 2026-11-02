"""Smart Matching tests: PS4 variants, GPU VRAM, storage, kit assessment."""
import pytest

from avitobot.normalizer import normalize_lot, _parse_storage_gb, _parse_gpu_vram


class TestStorage:
    @pytest.mark.parametrize("text,expected", [
        ("nvme 1TB", 1024),
        ("SSD 500GB", 512),
        ("ssd 256 гб", 256),
        ("жёсткий диск 1 ТБ", 1024),
        ("ps4 slim 500 гб", 512),
        ("128GB iphone", 128),
        ("без объёма тут", None),
    ])
    def test_storage_gb(self, text, expected):
        assert _parse_storage_gb(text) == expected


class TestGpuVram:
    @pytest.mark.parametrize("text,expected", [
        ("rtx 3060 12gb", 12),
        ("RTX 3060 8GB", 8),
        ("rtx 3070 ti 8gb vram", 8),
        ("gtx 1660 super 6gb", 6),
        ("rx 6700 xt 12 гб", 12),
        ("rtx 3060 без указания", None),
    ])
    def test_vram(self, text, expected):
        assert _parse_gpu_vram(text) == expected


class TestPS4Matching:
    def test_ps4_pro_1tb(self):
        lot = normalize_lot("Sony PlayStation 4 Pro 1TB в коробке")
        assert lot.model == "ps4"
        assert lot.revision == "pro"
        assert lot.memory_gb == 1024
        assert lot.assembly == "PS4 Pro / 1TB [коробка]"

    def test_ps4_slim_512(self):
        lot = normalize_lot("ps4 slim 500 гб 2 геймпада")
        assert lot.revision == "slim"
        assert lot.memory_gb == 512
        assert "2 геймпада" in lot.kit_label

    def test_ps4_fat_default(self):
        lot = normalize_lot("playstation 4 играми коробка")
        assert lot.revision == "fat"

    def test_configs_stay_separate(self):
        """Fat and Pro must never share a market bucket."""
        fat = normalize_lot("ps4 500 гб").market_key
        pro = normalize_lot("ps4 pro 1тб").market_key
        assert fat != pro
        assert "ps4" in fat and "::512" in fat


class TestRtxMatching:
    def test_rtx_3060_vram_separates(self):
        a = normalize_lot("RTX 3060 12GB").market_key
        b = normalize_lot("RTX 3060 8GB").market_key
        assert a != b
        assert a == "::".join(["rtx_3060", "", "12"])
        assert b == "::".join(["rtx_3060", "", "8"])


class TestKit:
    def test_full_kit_bonus(self):
        lot = normalize_lot("ps4 pro 1тб полный комплект 2 геймпада")
        assert lot.kit_bonus > 0
        assert lot.kit_penalty == 0

    def test_bare_console_penalty(self):
        lot = normalize_lot("ps4 fat 500гб тушка")
        assert lot.kit_penalty > 0
        assert lot.kit_bonus == -lot.kit_penalty  # net signed penalty
        assert "тушка" in lot.kit_label

    def test_no_kit_neutral(self):
        lot = normalize_lot("ps4 500 гб")
        assert lot.kit_bonus == 0
        assert lot.kit_penalty == 0


class TestUnknown:
    def test_unknown_model(self):
        lot = normalize_lot("что-то странное")
        assert lot.model == "unknown"
        assert "модель не определена" in lot.notes