# -*- coding: utf-8 -*-
"""自检脚本：用系统自带的语音合成（SAPI）朗读一段话。

声音会走默认播放设备，实时辅助字幕能把它当作"系统正在播放的声音"抓取到，
所以不用去找视频，就能验证「字幕 → 记事本」整条链路是否通了。

注意：朗读的语言必须和实时辅助字幕的字幕语言一致，否则识别不出来。
      Live Captions 的语言在「设置 → 辅助功能 → 实时辅助字幕」里看。

用法：
  python selftest.py --list                 # 列出本机可用语音
  python selftest.py                        # 用默认语音朗读默认文本
  python selftest.py --voice Hortense       # 指定语音（按名字里的关键字匹配）
  python selftest.py --text "自定义文本"
"""
from __future__ import annotations

import argparse
import sys

import comtypes.client as cc

DEFAULT_TEXT_FR = (
    "Bonjour. Ceci est un test de transcription en temps réel. "
    "Windows Live Captions devrait afficher ces phrases une par une. "
    "Un, deux, trois, quatre, cinq. Merci beaucoup et bonne journée."
)

DEFAULT_TEXT_ZH = (
    "你好。这是一段实时字幕转录测试。"
    "系统应该把这几句话逐句显示出来。一二三四五。谢谢，再见。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="用 SAPI 朗读一段话，供实时字幕抓取")
    parser.add_argument("--list", action="store_true", help="列出本机可用语音后退出")
    parser.add_argument("--voice", help="语音名字里包含该关键字就用它，例如 Hortense / Huihui / Zira")
    parser.add_argument("--text", help="要朗读的文本")
    parser.add_argument("--lang", choices=["fr", "zh"], default="fr",
                        help="默认文本的语言（fr=法语，zh=中文），需与字幕语言一致")
    args = parser.parse_args()

    voice = cc.CreateObject("SAPI.SpVoice")
    tokens = list(voice.GetVoices())

    if args.list:
        print("本机可用语音：")
        for i, t in enumerate(tokens):
            print(f"  [{i}] {t.GetDescription()}")
        return 0

    if args.voice:
        picked = None
        for t in tokens:
            if args.voice.lower() in t.GetDescription().lower():
                picked = t
                break
        if picked is None:
            print(f"[!] 没找到包含 {args.voice!r} 的语音，可用的是：")
            for t in tokens:
                print(f"    - {t.GetDescription()}")
            return 2
        voice.Voice = picked
    else:
        # 没指定就用默认语音
        picked = None

    desc = voice.Voice.GetDescription() if voice.Voice is not None else "(默认)"
    print(f"使用语音：{desc}")

    text = args.text or (DEFAULT_TEXT_FR if args.lang == "fr" else DEFAULT_TEXT_ZH)
    print(f"开始朗读（{len(text)} 字）：{text[:60]}…")
    print("提示：如果实时字幕的语言和这个语音不一致，是识别不出来的。")
    voice.Speak(text)
    print("朗读结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
