"""麦当劳餐品营养数据解析。

list-nutrition-foods 工具返回的原始文本中，核心数据块形如::

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      ...

由于 productName / nutritionDescription 可能包含逗号，这里从行尾向前解析
7 个数值字段，剩余部分再切分为名称与描述，保证解析稳健。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class FoodItem:
    """单个餐品的营养信息（每份）。"""

    name: str
    energy_kj: float
    energy_kcal: float
    protein: float          # 蛋白质 (g)
    fat: float              # 脂肪 (g)
    carbohydrate: float     # 碳水化合物 (g)
    sodium: float           # 钠 (mg)
    calcium: float          # 钙 (mg)
    description: Optional[str] = None

    @property
    def protein_per_100kcal(self) -> float:
        """每 100 千卡提供的蛋白质克数，衡量蛋白质密度。"""
        if self.energy_kcal <= 0:
            return 0.0
        return round(self.protein / self.energy_kcal * 100, 2)


# 数据块头部：[160]{...}:
_HEADER_RE = re.compile(r"\[(\d+)\]\{([^}]*)\}:")
_NUMERIC_FIELDS = 7  # energyKj, energyKcal, protein, fat, carbohydrate, sodium, calcium


def _to_float(token: str) -> float:
    token = token.strip()
    if token in ("", "null", "None", "-"):
        return 0.0
    try:
        return float(token)
    except ValueError:
        return 0.0


def parse_nutrition_text(raw: str) -> List[FoodItem]:
    """从 list-nutrition-foods 的原始文本中解析出餐品列表。

    对无法识别为数据行的内容静默跳过，保证健壮性。
    """
    items: List[FoodItem] = []
    seen = set()

    header = _HEADER_RE.search(raw)
    if header is None:
        return items

    # 数据区从 header 结束处开始。
    # 注意：麦当劳 MCP 返回时，数据块常被编码为单行，行分隔是字面量 "\n"（反斜杠+n），
    # 而非真实换行符；同时整体可能被 JSON 字符串的收尾 "} 包裹。这里统一规范化。
    body = raw[header.end():]
    body = body.split('"}')[0]        # 去掉可能的 JSON 收尾
    body = body.replace("\\n", "\n")   # 字面量 \n -> 真实换行
    for line in body.splitlines():
        line = line.strip().rstrip('"').strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        parts = line.split(",")
        if len(parts) < _NUMERIC_FIELDS + 1:
            # 至少要有 name + 7 个数值
            continue

        tail = parts[-_NUMERIC_FIELDS:]
        head = parts[: -_NUMERIC_FIELDS]

        # 尾部必须全是可解析的数字，否则不是数据行
        if not all(re.fullmatch(r"-?\d+(\.\d+)?|null|None|-", t.strip()) for t in tail):
            continue

        name = head[0].strip()
        description = None
        if len(head) >= 2:
            desc = ",".join(h.strip() for h in head[1:]).strip()
            if desc and desc.lower() != "null":
                description = desc
        if not name:
            continue

        energy_kj, energy_kcal, protein, fat, carb, sodium, calcium = (_to_float(t) for t in tail)

        key = (name, energy_kcal, protein)
        if key in seen:
            continue
        seen.add(key)

        items.append(
            FoodItem(
                name=name,
                energy_kj=energy_kj,
                energy_kcal=energy_kcal,
                protein=protein,
                fat=fat,
                carbohydrate=carb,
                sodium=sodium,
                calcium=calcium,
                description=description,
            )
        )

    return items
