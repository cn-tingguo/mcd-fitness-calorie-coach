"""套餐推荐引擎：在单餐热量预算内，搭配高蛋白、控脂控钠的麦当劳餐品组合。

策略：
1. 候选项中优先保留蛋白质密度（每 100kcal 蛋白质）较高的主食/蛋白来源。
2. 用有界的组合搜索（最多 N 件、限制每类重复）枚举候选组合。
3. 对每个组合按目标打分：越接近热量预算、蛋白质越达标越好；脂肪、钠超标则扣分。
4. 返回得分最高的若干组合。

搜索规模受控（候选裁剪 + 组合上限），保证单次推荐在毫秒级完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations_with_replacement
from typing import List, Sequence

from .fitness import NutritionTargets
from .nutrition import FoodItem

# 单餐钠上限参考（mg）：中国居民膳食指南每日钠建议约 < 2000mg（≈食盐 5g），
# 单餐按约 1/3 取 ~667mg 偏严格，这里取 700mg 作为软上限。
MEAL_SODIUM_SOFT_LIMIT = 700.0

# 同一餐品在一个组合中的最大份数：普通餐品最多 2 份，饮品类最多 1 份
MAX_SAME_ITEM = 2
MAX_SAME_DRINK = 1

# 饮品类关键词（用于限制重复份数，避免"两大杯饮料凑蛋白"这类不合理搭配）
# 注意：不使用过于宽泛的"杯"，否则会误伤"玉米杯"等小食；改用具体饮品名。
_DRINK_KEYWORDS = (
    "可乐", "雪碧", "咖啡", "美式", "拿铁", "奶铁", "红茶", "奶茶", "豆浆",
    "牛奶", "果汁", "柠檬", "怡泉", "橙", "卡布奇诺", "玛奇朵", "阿芙佳朵",
    "雪冰", "黑巧", "抹茶", "燕麦奶",
)

# 明确不属于饮品的例外（即使命中上面的关键词也不算饮品）
_DRINK_EXCEPTIONS = ("玉米杯",)


def _is_drink(item: FoodItem) -> bool:
    if any(ex in item.name for ex in _DRINK_EXCEPTIONS):
        return False
    return any(kw in item.name for kw in _DRINK_KEYWORDS)


def _max_count_for(item: FoodItem) -> int:
    return MAX_SAME_DRINK if _is_drink(item) else MAX_SAME_ITEM


@dataclass
class MealCombo:
    """一个推荐的餐品组合。"""

    items: List[FoodItem]
    calories: float
    protein: float
    fat: float
    carbohydrate: float
    sodium: float
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def item_names(self) -> List[str]:
        return [it.name for it in self.items]


def _aggregate(items: Sequence[FoodItem]) -> MealCombo:
    return MealCombo(
        items=list(items),
        calories=round(sum(i.energy_kcal for i in items), 1),
        protein=round(sum(i.protein for i in items), 1),
        fat=round(sum(i.fat for i in items), 1),
        carbohydrate=round(sum(i.carbohydrate for i in items), 1),
        sodium=round(sum(i.sodium for i in items), 1),
    )


def _score(combo: MealCombo, targets: NutritionTargets) -> float:
    """组合打分，越高越好。"""
    cal_target = max(targets.meal_calories, 1)
    protein_target = max(targets.meal_protein_g, 1)

    # 热量偏差惩罚：偏离预算越多扣分越多
    cal_dev = abs(combo.calories - cal_target) / cal_target
    cal_score = max(0.0, 1.0 - cal_dev) * 100

    # 蛋白质达标奖励：达到目标给满分，超一点不惩罚，差太多扣分
    protein_ratio = combo.protein / protein_target
    if protein_ratio >= 1.0:
        protein_score = 100.0
    else:
        protein_score = protein_ratio * 100

    # 脂肪控制：超过本餐脂肪目标按比例扣分
    fat_target = max(targets.meal_fat_g, 1)
    fat_penalty = max(0.0, (combo.fat - fat_target) / fat_target) * 40

    # 钠控制：超过软上限扣分
    sodium_penalty = max(0.0, (combo.sodium - MEAL_SODIUM_SOFT_LIMIT) / MEAL_SODIUM_SOFT_LIMIT) * 25

    # 加权：热量 0.9，蛋白质 1.1
    return round(cal_score * 0.9 + protein_score * 1.1 - fat_penalty - sodium_penalty, 2)


def _build_reasons(combo: MealCombo, targets: NutritionTargets) -> List[str]:
    reasons: List[str] = []
    cal_target = targets.meal_calories
    reasons.append(f"合计约 {combo.calories:.0f} kcal，本餐预算 {cal_target} kcal。")
    if combo.protein >= targets.meal_protein_g:
        reasons.append(f"蛋白质 {combo.protein:.0f}g，达到本餐目标 {targets.meal_protein_g}g，有利于{_goal_cn(targets.goal)}。")
    else:
        gap = targets.meal_protein_g - combo.protein
        reasons.append(f"蛋白质 {combo.protein:.0f}g，距本餐目标还差约 {gap:.0f}g，可额外加一份高蛋白小食。")
    if combo.fat > targets.meal_fat_g:
        reasons.append(f"脂肪 {combo.fat:.0f}g 略高于本餐目标 {targets.meal_fat_g}g，建议搭配运动日食用。")
    if combo.sodium > MEAL_SODIUM_SOFT_LIMIT:
        reasons.append(f"钠约 {combo.sodium:.0f}mg 偏高，当天其余餐食注意清淡。")
    return reasons


def _goal_cn(goal: str) -> str:
    return {"cut": "减脂保肌", "bulk": "增肌", "maintain": "维持体重"}.get(goal, "控制体重")


def _prefilter(
    foods: Sequence[FoodItem],
    targets: NutritionTargets,
    exclude_keywords: Sequence[str],
) -> List[FoodItem]:
    """裁剪候选集：剔除排除项、单品热量过高/为 0 的项，优先保留蛋白质密度高的。"""
    cal_cap = targets.meal_calories * 1.1  # 单品不应超过本餐预算太多
    kept: List[FoodItem] = []
    for f in foods:
        if f.energy_kcal <= 0:
            continue
        if f.energy_kcal > cal_cap:
            continue
        if any(kw and kw in f.name for kw in exclude_keywords):
            continue
        kept.append(f)

    # 按蛋白质密度降序，取前 N 作为主要候选，控制组合搜索规模
    kept.sort(key=lambda x: (x.protein_per_100kcal, x.protein), reverse=True)
    return kept[:40]


def recommend_combos(
    foods: Sequence[FoodItem],
    targets: NutritionTargets,
    max_items: int = 3,
    top_n: int = 3,
    exclude_keywords: Sequence[str] = (),
) -> List[MealCombo]:
    """生成 top_n 个推荐组合。

    max_items: 组合中最多包含的餐品件数（含重复）。
    exclude_keywords: 名称中包含这些关键词的餐品会被排除（如忌口 "可乐"）。
    """
    candidates = _prefilter(foods, targets, exclude_keywords)
    if not candidates:
        return []

    cal_target = max(targets.meal_calories, 1)
    upper = cal_target * 1.25  # 允许的组合热量上限

    scored: List[MealCombo] = []
    seen = set()

    for size in range(1, max_items + 1):
        for group in combinations_with_replacement(candidates, size):
            # 限制同一餐品的重复份数（饮品最多 1 份，其余最多 2 份）
            counts: dict = {}
            over_limit = False
            for it in group:
                counts[it.name] = counts.get(it.name, 0) + 1
                if counts[it.name] > _max_count_for(it):
                    over_limit = True
                    break
            if over_limit:
                continue

            total_cal = sum(i.energy_kcal for i in group)
            if total_cal > upper:
                continue
            if total_cal < cal_target * 0.55:
                # 太少不作为完整一餐
                continue
            key = tuple(sorted(i.name for i in group))
            if key in seen:
                continue
            seen.add(key)

            combo = _aggregate(group)
            combo.score = _score(combo, targets)
            scored.append(combo)

    scored.sort(key=lambda c: c.score, reverse=True)
    results = scored[:top_n]
    for combo in results:
        combo.reasons = _build_reasons(combo, targets)
    return results
