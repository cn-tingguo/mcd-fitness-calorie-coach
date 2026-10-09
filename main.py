#!/usr/bin/env python3
"""麦当劳健身卡路里教练 —— 命令行 Skill 入口。

根据身体数据与健身目标，调用麦当劳官方 MCP 的 list-nutrition-foods 获取真实餐品
营养数据，推荐符合单餐卡路里与蛋白质目标的麦当劳套餐组合。

用法::

    export MCD_MCP_TOKEN=你的Token          # 或写入项目根目录 .env
    python main.py --gender male --age 28 --height 178 --weight 80 \
        --activity moderate --goal cut --meals 3 --exclude 可乐 雪碧

查看全部参数::

    python main.py --help
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# 允许从任意工作目录运行：将脚本所在目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.mcd_client import McdMcpClient, McdMcpError
from src.nutrition import parse_nutrition_text
from src.fitness import UserProfile, calculate_targets
from src.recommender import recommend_combos


def _load_dotenv() -> None:
    """极简 .env 加载：若存在项目根目录 .env，则把其中变量注入环境（不覆盖已有）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _goal_cn(goal: str) -> str:
    return {"cut": "减脂", "bulk": "增肌", "maintain": "维持"}.get(goal, goal)


def _extract_formatted_time(raw: str) -> str:
    """从 now-time-info 文本中提取 formatted 时间 (yyyy-MM-dd HH:mm:ss)。失败则返回空串。"""
    m = re.search(r'"formatted"\s*:\s*"([^"]+)"', raw)
    return m.group(1) if m else ""


def _meal_segment(formatted: str) -> str:
    """根据小时判断餐段，用于提示。"""
    m = re.search(r"\s(\d{1,2}):", formatted)
    if not m:
        return ""
    hour = int(m.group(1))
    if 5 <= hour < 10:
        return "早餐时段"
    if 10 <= hour < 14:
        return "午餐时段"
    if 14 <= hour < 17:
        return "下午茶时段"
    if 17 <= hour < 21:
        return "晚餐时段"
    return "夜宵时段"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-fitness-coach",
        description="麦当劳健身卡路里教练：按健身目标推荐符合卡路里/蛋白质目标的麦当劳套餐。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--gender", default="male", help="性别：male/female 或 男/女")
    parser.add_argument("--age", type=int, default=28, help="年龄（岁）")
    parser.add_argument("--height", type=float, default=178, help="身高（cm）")
    parser.add_argument("--weight", type=float, default=80, help="体重（kg）")
    parser.add_argument("--body-fat", type=float, default=None, metavar="百分比",
                        help="体脂率(%%)，可选；提供后按瘦体重计算蛋白质，更精确")
    parser.add_argument(
        "--activity", default="moderate",
        choices=["sedentary", "light", "moderate", "active", "very_active"],
        help="活动水平",
    )
    parser.add_argument(
        "--goal", default="cut", choices=["cut", "maintain", "bulk"],
        help="健身目标：cut 减脂 / maintain 维持 / bulk 增肌",
    )
    parser.add_argument("--meals", type=int, default=3, help="每天餐数（用于分配单餐热量）")
    parser.add_argument("--max-items", type=int, default=3, help="单餐最多搭配餐品件数")
    parser.add_argument("--top", type=int, default=3, help="返回的推荐方案数量")
    parser.add_argument("--exclude", nargs="*", default=[], metavar="关键词",
                        help="忌口关键词，名称含这些词的餐品会被排除，如：可乐 薯条")
    return parser


def run(args: argparse.Namespace) -> int:
    profile = UserProfile(
        gender=args.gender, age=args.age, height_cm=args.height, weight_kg=args.weight,
        activity_level=args.activity, goal=args.goal, body_fat_pct=args.body_fat,
    )
    targets = calculate_targets(profile, meal_fraction=1.0 / max(args.meals, 1))

    p = profile.normalized()
    print("=" * 60)
    print("  🍔  麦当劳健身卡路里教练")
    print("=" * 60)
    print(f"用户: {p.gender} / {p.age}岁 / {p.height_cm:.0f}cm / {p.weight_kg:.0f}kg "
          f"/ 活动:{p.activity_level} / 目标:{_goal_cn(p.goal)}")
    print(f"BMR 基础代谢: {targets.bmr:.0f} kcal/日")
    print(f"TDEE 每日消耗: {targets.tdee:.0f} kcal/日")
    print(f"每日目标: {targets.daily_calories} kcal | 蛋白 {targets.daily_protein_g}g "
          f"| 脂肪 {targets.daily_fat_g}g | 碳水 {targets.daily_carb_g}g")
    print(f"单餐目标(1/{args.meals}): {targets.meal_calories} kcal | 蛋白 {targets.meal_protein_g}g "
          f"| 脂肪 {targets.meal_fat_g}g | 碳水 {targets.meal_carb_g}g")
    if targets.protein_basis == "lean_body_mass":
        basis_cn = "瘦体重(更精确)"
    else:
        basis_cn = "总体重估算"
    print(f"蛋白质计算依据: {basis_cn}")
    for w in targets.warnings:
        print(f"⚠️  {w}")
    print("-" * 60)

    try:
        client = McdMcpClient.from_env()
        client.initialize()
        # 调用 now-time-info 获取当前时间，用于提示当前餐段
        time_raw = client.now_time_info()
        raw = client.list_nutrition_foods()
    except McdMcpError as exc:
        print(f"[错误] {exc}")
        print("提示：请确认已设置环境变量 MCD_MCP_TOKEN（或在项目根目录 .env 中配置）。")
        return 1

    formatted = _extract_formatted_time(time_raw)
    if formatted:
        segment = _meal_segment(formatted)
        print(f"当前时间: {formatted}" + (f"（{segment}）" if segment else ""))

    foods = parse_nutrition_text(raw)
    print(f"已从麦当劳 MCP 获取 {len(foods)} 条真实餐品营养数据。")
    print("-" * 60)

    combos = recommend_combos(
        foods, targets,
        max_items=args.max_items, top_n=args.top,
        exclude_keywords=tuple(args.exclude),
    )
    if not combos:
        print("未找到合适的组合，可尝试放宽忌口或调整每日餐数。")
        return 0

    for i, combo in enumerate(combos, 1):
        print(f"\n推荐方案 #{i}  (匹配分 {combo.score})")
        print(f"  餐品: {' + '.join(combo.item_names)}")
        print(f"  合计: {combo.calories:.0f} kcal | 蛋白 {combo.protein:.0f}g "
              f"| 脂肪 {combo.fat:.0f}g | 碳水 {combo.carbohydrate:.0f}g | 钠 {combo.sodium:.0f}mg")
        for r in combo.reasons:
            print(f"  · {r}")

    print("\n" + "-" * 60)
    print("※ 结果仅供参考，不构成医疗或营养专业建议；餐品及供应状态以麦当劳官方渠道为准。")
    return 0


def main() -> int:
    _load_dotenv()
    args = _build_parser().parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
