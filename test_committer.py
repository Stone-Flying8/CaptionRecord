# -*- coding: utf-8 -*-
"""定稿判定逻辑的回归测试。

用的数据是真实跑出来的：下面这串 raw 文本是 2026-09-12 实测 Live Captions
（Windows 11 build 26200）在朗读一句法语时，CaptionsTextBlock 的 Name 随时间的变化。

直接运行即可：python test_committer.py
"""
from __future__ import annotations

import types

import live_captions_scribe as lcs

U1 = "Bonjour, ceci est un test de transcription en temps réel"
U2 = "Windows Live Caption devrait afficher ces phrases une par une."
U3 = "12345."
U4 = "Merci beaucoup et bonne journée"

# (相对时间秒, 文本块内容) —— 完全按实测日志还原
SEQUENCE = [
    (0.0, ""),
    (2.0, "Bonjour"),
    (4.0, "Bonjour, ceci"),
    (4.5, "Bonjour, ceci est un"),
    (5.0, "Bonjour, ceci est un t"),
    (5.5, "Bonjour, ceci est un test de"),
    (6.0, "Bonjour, ceci est un test de trans"),
    (6.5, "Bonjour, ceci est un test de transcription"),
    (7.0, "Bonjour, ceci est un test de transcription en temps"),
    (7.5, U1),
    (8.0, U1 + "\nWind"),
    (8.5, U1 + "\nWindows"),
    (9.0, U1 + "\nWindows Live"),
    (9.5, U1 + "\nWindows Live Caption"),
    (10.0, U1 + "\nWindows Live Caption devrait"),
    (10.5, U1 + "\nWindows Live Caption devrait afficher"),
    (11.0, U1 + "\nWindows Live Caption devrait afficher ces phrases"),
    (11.5, U1 + "\nWindows Live Caption devrait afficher ces phrases une par une"),
    (12.0, U1 + "\n" + U2),
    (14.0, U1 + "\n" + U2 + "\n123"),
    (15.0, U1 + "\n" + U2 + "\n12345"),
    (16.0, U1 + "\n" + U2 + "\n" + U3),
    (17.0, U1 + "\n" + U2 + "\n" + U3 + "\nMerci beaucoup"),
    (18.0, U1 + "\n" + U2 + "\n" + U3 + "\nMerci beaucoup et"),
    (19.0, U1 + "\n" + U2 + "\n" + U3 + "\n" + U4),
]

EXPECTED = [U1, U2, U3, U4]


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t


def run(sequence=None, idle_flush=6.0, settle_ms=1200.0, flush_raw=None) -> list[str]:
    clock = FakeClock()
    # 把模块里的 time 换掉，就能精确控制"经过了多少秒"
    original = lcs.time
    lcs.time = types.SimpleNamespace(time=clock.time, sleep=lambda *_: None)
    try:
        committer = lcs.Committer(idle_flush=idle_flush, settle_ms=settle_ms)
        seq = SEQUENCE if sequence is None else sequence
        out: list[str] = []
        for t, raw in seq:
            clock.t = t
            out.extend(committer.feed(raw))
        clock.t = (seq[-1][0] if seq else 0.0) + 0.5
        out.extend(committer.flush(flush_raw if flush_raw is not None else seq[-1][1]))
        return out
    finally:
        lcs.time = original


# 场景二：短句在"稳定期"到期前就被顶出字幕块（实测踩到过，会永久丢句）
# "Bonjour." 只活了 0.4 秒就被下一句顶掉，但绝不能丢
SCROLL_OUT = [
    (0.0, ""),
    (1.0, "Bonjour."),
    (1.4, "Bonjour.\nBienvenue dans ce test"),
    (1.8, "Bienvenue dans ce test\nde transcription"),
    (3.5, "Bienvenue dans ce test\nde transcription."),
]


def main() -> int:
    got = run()
    print("场景一（实测序列）实际输出：")
    for s in got:
        print(f"  ✓ {s}")

    ok = True
    if got != EXPECTED:
        ok = False
        print("\n[失败] 与预期不一致。")
        print("预期：")
        for s in EXPECTED:
            print(f"  · {s}")
        missing = [s for s in EXPECTED if s not in got]
        extra = [s for s in got if s not in EXPECTED]
        if missing:
            print("  缺失：", missing)
        if extra:
            print("  多余：", extra)
        if len(got) != len(set(got)):
            print("  存在重复项")
    else:
        print("\n[通过] 定稿判定、去重、滚动裁剪、退出收尾全部符合预期。")

    # 额外检查：不该出现"半截句"
    for s in got:
        if s.startswith("Bonjour, ceci est un test de transcription en temps réel Windows"):
            ok = False
            print("[失败] 出现了把两个单元粘在一起的错误拼接")

    # ---- 场景二：被提前顶出字幕块的短句不能丢 ----
    print("\n场景二（短句被提前顶出）实际输出：")
    got2 = run(sequence=SCROLL_OUT, flush_raw=SCROLL_OUT[-1][1])
    for s in got2:
        print(f"  ✓ {s}")
    if "Bonjour." not in got2:
        ok = False
        print("[失败] 被提前顶出字幕块的短句 'Bonjour.' 丢了")
    else:
        print("[通过] 被提前顶出的短句被成功补输出，没有丢。")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
