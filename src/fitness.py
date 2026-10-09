"""健身营养计算：BMR、TDEE、单餐卡路里与宏量营养目标。

公式与循证依据：
- BMR 采用 Mifflin-St Jeor 公式（学界较通用、误差较小）。
- TDEE = BMR × 活动系数。
- 热量调整：减脂 (cut) 制造缺口、增肌 (bulk) 制造盈余、维持 (maintain) 持平。
- 蛋白质按"用于计算的体重"设定；**高体脂人群应按瘦体重/目标体重计**，
  因此当提供 body_fat_pct 时优先用瘦体重 (LBM)，否则对蛋白质总量做合理封顶，
  避免按总体重高估（如 ISSN 立场声明、Helms 等 meta 的做法）。
- 安全下限：减脂热量不低于 max(BMR, 性别最低安全摄入)，防止代谢适应与肌肉流失。
- 脂肪：取 max(占总热量比例, 每公斤下限)，保护激素健康。

本模块只做计算，不涉及任何网络或业务数据，便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple

# 活动系数（久坐 -> 极高强度）
ACTIVITY_FACTORS: Dict[str, float] = {
    "sedentary": 1.2,      # 久坐，几乎不运动
    "light": 1.375,        # 每周轻度运动 1-3 天
    "moderate": 1.55,      # 每周中等运动 3-5 天
    "active": 1.725,       # 每周高强度运动 6-7 天
    "very_active": 1.9,    # 体力劳动 / 每天高强度训练
}

# 健身目标 -> 每日热量相对 TDEE 的调整比例
GOAL_CALORIE_FACTORS: Dict[str, float] = {
    "cut": 0.80,       # 减脂：约 20% 热量缺口
    "maintain": 1.0,   # 维持
    "bulk": 1.12,      # 增肌：约 12% 热量盈余
}

# 健身目标 -> 每公斤（用于计算的）体重蛋白质目标 (g/kg)
GOAL_PROTEIN_PER_KG: Dict[str, float] = {
    "cut": 2.2,
    "maintain": 1.8,
    "bulk": 2.0,
}

# 蛋白质总量封顶（当以总体重估算、且无体脂数据时的保护上限）：
# 循证上蛋白质增益约在 1.6~2.2 g/kg 瘦体重后趋于平台；对以总体重估算、
# 体脂未知者，按总体重算易高估，故同时施加 g/kg 上限与绝对克数上限。
PROTEIN_CAP_PER_KG = 2.4
PROTEIN_ABS_CAP_G = 200.0  # 绝对上限：多数健身人群每日蛋白超过此值无额外增益

# 性别最低安全每日摄入（kcal），常见临床保守下限，低于此不利于营养均衡
SAFE_MIN_CALORIES: Dict[str, float] = {"male": 1500.0, "female": 1200.0}

# 脂肪：占总热量比例 + 每公斤体重下限（保护激素健康）
FAT_CALORIE_RATIO = 0.25
FAT_MIN_PER_KG = 0.8

KCAL_PER_G_PROTEIN = 4
KCAL_PER_G_CARB = 4
KCAL_PER_G_FAT = 9

# 极端输入护栏阈值
BMI_LOW = 16.0
BMI_HIGH = 40.0
AGE_MIN = 15
AGE_MAX = 80


@dataclass
class UserProfile:
    """用户身体与训练信息。"""

    gender: str          # "male" / "female"
    age: int
    height_cm: float
    weight_kg: float
    activity_level: str = "moderate"
    goal: str = "cut"    # "cut" / "maintain" / "bulk"
    body_fat_pct: Optional[float] = None  # 体脂率(%)，提供时用于计算瘦体重

    def normalized(self) -> "UserProfile":
        gender = self.gender.strip().lower()
        if gender in ("男", "m", "male"):
            gender = "male"
        elif gender in ("女", "f", "female"):
            gender = "female"
        activity = self.activity_level.strip().lower()
        if activity not in ACTIVITY_FACTORS:
            activity = "moderate"
        goal = self.goal.strip().lower()
        if goal not in GOAL_CALORIE_FACTORS:
            goal = "maintain"
        bf = self.body_fat_pct
        if bf is not None:
            bf = float(bf)
            if bf <= 0 or bf >= 75:  # 明显异常值直接忽略
                bf = None
        return UserProfile(
            gender=gender,
            age=int(self.age),
            height_cm=float(self.height_cm),
            weight_kg=float(self.weight_kg),
            activity_level=activity,
            goal=goal,
            body_fat_pct=bf,
        )

    @property
    def bmi(self) -> float:
        h = self.height_cm / 100.0
        if h <= 0:
            return 0.0
        return round(self.weight_kg / (h * h), 1)


@dataclass
class NutritionTargets:
    """计算得到的每日 / 单餐营养目标。"""

    bmr: float
    tdee: float
    daily_calories: float
    daily_protein_g: float
    daily_fat_g: float
    daily_carb_g: float
    meal_calories: float
    meal_protein_g: float
    meal_fat_g: float
    meal_carb_g: float
    goal: str
    meal_fraction: float
    protein_basis: str = "total_weight"   # 蛋白质计算依据：lean_body_mass / total_weight
    calorie_floor_applied: bool = False    # 是否触发安全下限
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def calculate_bmr(profile: UserProfile) -> float:
    """Mifflin-St Jeor 公式计算基础代谢率 (kcal/day)。"""
    p = profile.normalized()
    base = 10 * p.weight_kg + 6.25 * p.height_cm - 5 * p.age
    if p.gender == "male":
        return base + 5
    return base - 161


def calculate_tdee(profile: UserProfile) -> float:
    """每日总能量消耗 (kcal/day)。"""
    p = profile.normalized()
    return calculate_bmr(p) * ACTIVITY_FACTORS[p.activity_level]


def _lean_body_mass(p: UserProfile) -> Optional[float]:
    """有体脂率时估算瘦体重 (kg)。"""
    if p.body_fat_pct is None:
        return None
    return p.weight_kg * (1 - p.body_fat_pct / 100.0)


def _protein_target(p: UserProfile) -> Tuple[float, str]:
    """计算每日蛋白质目标 (g) 及其计算依据。

    - 有体脂率：按瘦体重 × (per_kg + 一定上浮) 计，更精确。
    - 无体脂率：按总体重 × per_kg，但封顶在 总体重 × PROTEIN_CAP_PER_KG，
      避免对高体脂人群按总体重严重高估。
    """
    per_kg = GOAL_PROTEIN_PER_KG[p.goal]
    lbm = _lean_body_mass(p)
    if lbm is not None:
        # 瘦体重基础上，蛋白质系数相对总体重法略高（因基数更小），约 +0.3 g/kg LBM
        grams = lbm * (per_kg + 0.3)
        grams = min(grams, p.weight_kg * PROTEIN_CAP_PER_KG, PROTEIN_ABS_CAP_G)
        return round(grams), "lean_body_mass"
    grams = min(per_kg * p.weight_kg, PROTEIN_CAP_PER_KG * p.weight_kg, PROTEIN_ABS_CAP_G)
    return round(grams), "total_weight"


def _health_warnings(p: UserProfile) -> List[str]:
    warns: List[str] = []
    bmi = p.bmi
    if bmi and bmi < BMI_LOW:
        warns.append(f"BMI 约 {bmi}，偏低。减脂可能不适合，建议咨询医生或营养师。")
    elif bmi and bmi > BMI_HIGH:
        warns.append(f"BMI 约 {bmi}，偏高。建议在专业人士指导下制定减重计划。")
    if p.age < AGE_MIN:
        warns.append(f"年龄 {p.age} 岁偏小，青少年营养需求特殊，建议遵医嘱，本结果仅供参考。")
    elif p.age > AGE_MAX:
        warns.append(f"年龄 {p.age} 岁，老年人代谢与营养需求差异较大，建议结合医嘱。")
    return warns


def calculate_targets(profile: UserProfile, meal_fraction: float = 1.0 / 3.0) -> NutritionTargets:
    """根据用户信息与本餐占全天比例，计算营养目标。

    meal_fraction: 本餐热量占全天的比例，默认 1/3（三餐之一）。
    """
    p = profile.normalized()
    bmr = round(calculate_bmr(p), 1)
    tdee = round(calculate_tdee(p), 1)

    # 1) 目标热量 + 安全下限
    # 安全下限对所有目标生效：无论减脂/维持/增肌，每日热量都不应低于
    # max(BMR, 性别最低安全摄入)，避免给出不健康的过低热量建议。
    raw_daily_cal = tdee * GOAL_CALORIE_FACTORS[p.goal]
    floor = max(bmr, SAFE_MIN_CALORIES.get(p.gender, 1200.0))
    calorie_floor_applied = False
    daily_cal = raw_daily_cal
    if raw_daily_cal < floor:
        daily_cal = floor
        calorie_floor_applied = True

    # 2) 蛋白质（封顶 / 瘦体重）
    daily_protein, protein_basis = _protein_target(p)
    protein_capped = (
        protein_basis == "total_weight"
        and daily_protein >= min(PROTEIN_CAP_PER_KG * p.weight_kg, PROTEIN_ABS_CAP_G) - 0.5
        and GOAL_PROTEIN_PER_KG[p.goal] * p.weight_kg > daily_protein + 0.5
    )

    # 3) 脂肪：max(比例供能, 每公斤下限)
    fat_by_ratio = daily_cal * FAT_CALORIE_RATIO / KCAL_PER_G_FAT
    fat_floor = FAT_MIN_PER_KG * p.weight_kg
    daily_fat = max(fat_by_ratio, fat_floor)

    # 4) 碳水 = 剩余热量；若被蛋白+脂肪挤占，则兜底为 0 并给出提示
    protein_cal = daily_protein * KCAL_PER_G_PROTEIN
    fat_cal = daily_fat * KCAL_PER_G_FAT
    carb_cal = daily_cal - protein_cal - fat_cal
    carb_warn = carb_cal < 0.10 * daily_cal  # 碳水占比过低
    carb_cal = max(carb_cal, 0.0)
    daily_carb = carb_cal / KCAL_PER_G_CARB

    frac = max(min(meal_fraction, 1.0), 0.05)

    warnings = _health_warnings(p)
    if calorie_floor_applied:
        warnings.append(
            f"按目标计算的热量低于安全下限，已上调至 {round(daily_cal)} kcal "
            f"(不低于 BMR/性别最低值)，避免代谢损伤。"
        )
    if protein_capped:
        warnings.append(
            "蛋白质目标已按上限封顶（未提供体脂率时按总体重估算易高估）；"
            "若体脂偏高，建议补充体脂率(--body-fat)以按瘦体重更精确计算。"
        )
    if carb_warn:
        warnings.append("蛋白质与脂肪目标已接近总热量，碳水偏低；若日常训练量大可适当上调总热量。")

    return NutritionTargets(
        bmr=bmr,
        tdee=tdee,
        daily_calories=round(daily_cal),
        daily_protein_g=round(daily_protein),
        daily_fat_g=round(daily_fat),
        daily_carb_g=round(daily_carb),
        meal_calories=round(daily_cal * frac),
        meal_protein_g=round(daily_protein * frac),
        meal_fat_g=round(daily_fat * frac),
        meal_carb_g=round(daily_carb * frac),
        goal=p.goal,
        meal_fraction=round(frac, 3),
        protein_basis=protein_basis,
        calorie_floor_applied=calorie_floor_applied,
        warnings=warnings,
    )
