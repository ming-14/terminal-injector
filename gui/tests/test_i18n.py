# test_i18n.py — 中英语典键一致性与占位符一致性
#
# i18n.py 开头的硬约定:「zh/en 两份词典的键必须完全一致;缺键时 _t() 返回
# 键名本身」。缺键不会报错、只会把英文系统的界面显示成 key 名,所以必须靠
# 测试守 —— 手工比对过一次不算数。
#
# 除键集合外还比占位符:同一键两边 `{}` / `{name}` 数量或名字不同,则其中
# 一种语言下 .format() 必然抛 KeyError/IndexError。
#
# 运行:python -m unittest discover -s tests -t . -v  (或直接 python tests/test_i18n.py)

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

# 项目根目录:与 tests/__init__.py 的引导重复,保证两种启动方式都可用
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tigui import i18n                          # noqa: E402
from tigui.i18n import _t                       # noqa: E402

# {name} 或 {},不含转义 {{}}
_PLACEHOLDER = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")


def placeholders(text):
    """文案里的占位符列表(顺序敏感);{} 归一成空名便于跨语言比较"""
    return [_norm(p) for p in _PLACEHOLDER.findall(text)]


def _norm(name):
    return name.strip() or ""


class LangKeyParityTest(unittest.TestCase):
    """zh / en 键集合必须完全一致"""

    def test_key_sets_are_identical(self):
        zh = set(i18n._STR["zh"])
        en = set(i18n._STR["en"])
        self.assertEqual(zh - en, set(), "zh 有而 en 缺的键")
        self.assertEqual(en - zh, set(), "en 有而 zh 缺的键")

    def test_no_empty_values(self):
        for lang, table in i18n._STR.items():
            for key, val in table.items():
                self.assertTrue(str(val).strip(),
                                "%s.%s 文案为空" % (lang, key))


class PlaceholderParityTest(unittest.TestCase):
    """同一键两边的占位符必须对得上,否则一种语言下 .format() 会炸"""

    def test_placeholders_match_per_key(self):
        zh, en = i18n._STR["zh"], i18n._STR["en"]
        for key in sorted(set(zh) & set(en)):
            self.assertEqual(
                placeholders(zh[key]), placeholders(en[key]),
                "占位符不一致: %s\n  zh: %r\n  en: %r"
                % (key, zh[key], en[key]))


class ResolveTest(unittest.TestCase):
    """_t 的兜底行为:缺键返回键名、未知语言退回英文"""

    def test_missing_key_returns_key_name(self):
        self.assertEqual(_t("__no_such_key__"), "__no_such_key__")

    def test_unknown_language_falls_back_to_en(self):
        with mock.patch.object(i18n, "_LANG", "xx"):
            self.assertEqual(_t("title"), i18n._STR["en"]["title"])

    def test_all_keys_resolvable_in_both_langs(self):
        # 遍历两个词典的全部键:任何一边缺失都会返回键名,这里要求真拿到文案
        for lang in ("zh", "en"):
            with mock.patch.object(i18n, "_LANG", lang):
                for key in i18n._STR[lang]:
                    self.assertNotEqual(
                        _t(key), key, "%s 下 %s 未命中(缺键会返回键名)" % (lang, key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
