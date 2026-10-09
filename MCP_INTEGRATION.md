# MCP 集成说明

本文档说明本项目实际使用的麦当劳 MCP Server、Tool、调用流程及业务价值。

## 一、使用的麦当劳 MCP Server

- **Server 名称**：mcd-mcp（麦当劳中国官方 MCP Server）
- **接入地址**：`https://mcp.mcd.cn`
- **传输协议**：Streamable HTTP（JSON-RPC 2.0）
- **鉴权方式**：请求头 `Authorization: Bearer <MCD_MCP_TOKEN>`
- **Token 来源**：麦当劳 MCP 开放平台 <https://open.mcd.cn/mcp> 控制台激活获取
- **开放平台使用指南**：<https://github.com/M-China/mcd-mcp-server>

> Token 仅通过环境变量 `MCD_MCP_TOKEN` 注入，不在仓库任何位置硬编码。

## 二、实际调用的麦当劳 MCP Tool

| Tool | 名称 | 本项目的用途 |
| --- | --- | --- |
| `list-nutrition-foods` | 餐品营养信息列表 | **核心数据源**。获取麦当劳餐品的能量(kcal)、蛋白质、脂肪、碳水、钠、钙，用于单餐搭配计算与蛋白质密度排序。 |
| `now-time-info` | 获取当前时间信息 | 用于判断当前餐段（可扩展：早餐/正餐时段的差异化推荐），并作为连通性自检。 |

> 其余麦当劳 MCP Tool（如 `query-meals`、`query-store-coupons`）为后续可扩展方向，当前版本未纳入必需调用路径。

## 三、调用流程

```
用户输入身体数据与健身目标
        │
        ▼
[本项目] 计算 BMR / TDEE / 单餐热量与宏量营养目标 (fitness.py)
        │
        ▼
[本项目 -> 麦当劳 MCP] initialize 握手
        │
        ▼
[本项目 -> 麦当劳 MCP] tools/call: list-nutrition-foods
        │  返回 160+ 条真实餐品营养文本
        ▼
[本项目] 解析营养文本为结构化数据 (nutrition.py)
        │
        ▼
[本项目] 在单餐热量预算内搜索高蛋白、控脂控钠的组合并打分 (recommender.py)
        │
        ▼
返回 Top-N 推荐套餐组合 + 营养解释
```

JSON-RPC 调用示例（简化）：

```jsonc
// 1) 握手
{ "jsonrpc": "2.0", "id": 1, "method": "initialize",
  "params": { "protocolVersion": "2024-11-05", "capabilities": {},
              "clientInfo": { "name": "mcd-fitness-calorie-coach", "version": "1.0.0" } } }

// 2) 获取餐品营养数据
{ "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": { "name": "list-nutrition-foods", "arguments": {} } }
```

实现位置：`src/mcd_client.py`（`McdMcpClient.call_tool` / `list_nutrition_foods`）。

## 四、业务价值

- **解决真实痛点**：健身人群（减脂/增肌/维持）在外食时难以快速判断麦当劳餐品是否符合当日卡路里与蛋白质目标。
- **真实数据驱动**：所有营养数据均来自麦当劳官方 MCP 的 `list-nutrition-foods`，而非估算或硬编码，保证准确性与时效性。
- **可量化推荐**：基于 Mifflin-St Jeor 公式与宏量营养配比，给出"单餐热量预算 + 蛋白质目标"，再用组合优化在真实菜单内搭配，并以匹配分、脂肪/钠提示解释每个方案。
- **专业安全护栏**：加入热量安全下限（不低于基础代谢）、蛋白质按瘦体重计算/封顶、脂肪每公斤下限、极端 BMI 与年龄提醒，避免给出不健康的极端建议。
- **轻量可扩展**：以命令行 Skill 形式直接调用麦当劳 MCP，开箱即用；后续可接入 `query-meals`（在售菜单）与 `query-store-coupons`（省钱搭配）形成"健康 + 省钱"闭环。

## 五、项目形态

本项目是一个**直接调用麦当劳 MCP 的命令行 Skill**（而非再对外封装 MCP Server）。
运行入口为 `main.py`，内部通过 `src/mcd_client.py` 以 Streamable HTTP 调用麦当劳官方 MCP 工具，
再由 `src/fitness.py` 与 `src/recommender.py` 完成营养目标计算与套餐推荐。

## 六、限流与错误处理

- 遵守麦当劳 MCP 的 600 次/分钟限流；命令行 Skill 为一次性调用，每次运行仅发起少量请求（`now-time-info` + `list-nutrition-foods`）。
- 对 `401`（Token 无效）、`429`（限流）等错误做了显式提示（见 `src/mcd_client.py`）。
