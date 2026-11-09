# AI 代码安全审计平台 · 后端 MVP

> 技术栈：Python 3.11+ / FastAPI / SQLite（+ SQLAlchemy 2.x）
> 当前阶段：**第 2 步 — Web 框架跑通**（`/health` 可用、启动自动建表、CORS 已配置；业务接口待第 3 步）

## 快速开始

```powershell
python -m pip install -r requirements.txt      # 1. 装依赖
python -m app.db.init_db                       # 2. 建库（可跳过，启动时也会自动建）
uvicorn app.main:app --reload                  # 3. 起服务
```

打开 <http://127.0.0.1:8000/docs> 看交互式文档，或验证：

```powershell
curl http://127.0.0.1:8000/health
# {"status":"ok","database":"ok","version":"0.2.0","env":"development"}
```

跑测试：`python -m pytest`


## 目录结构

```
cook/
├── schema.sql                  # ★ 建表 SQL（唯一事实来源：表/索引/触发器/视图/种子数据）
├── requirements.txt            # 运行时依赖
├── requirements-dev.txt        # 测试与代码质量依赖
├── pytest.ini                  # pytest 配置
├── .env.example                # 环境变量样例（复制为 .env 使用）
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                 # ★ FastAPI 实例、CORS、lifespan（启动自动建表）、/health
│   ├── core/                   # 横切能力（最底层，不依赖业务）
│   │   ├── config.py           # ★ pydantic-settings 读取 .env
│   │   ├── database.py         # ★ engine / SessionLocal / get_db、SQLite PRAGMA
│   │   ├── exceptions.py       # 统一异常与错误响应格式（待实现）
│   │   └── logging.py          # 结构化日志
│   ├── models/                 # SQLAlchemy ORM，与 schema.sql 一一对应
│   │   ├── recipe.py           # recipes
│   │   ├── ingredient.py       # ingredients
│   │   └── recipe_ingredient.py# recipe_ingredients（配料比例）
│   ├── schemas/                # Pydantic 出入参（API 契约）
│   │   ├── recipe.py           # RecipeCreate / RecipeOut / RecipeDetailOut
│   │   └── ingredient.py
│   ├── api/
│   │   ├── deps.py             # 公共依赖（分页、DB 会话、鉴权占位）
│   │   └── v1/
│   │       ├── router.py       # 汇总各 endpoint 的 APIRouter
│   │       └── endpoints/
│   │           ├── recipes.py          # 食谱 CRUD、按份数换算配料
│   │           ├── ingredients.py      # 食材库维护、按名称模糊查询
│   │           └── shopping_list.py    # 选多道菜 -> 生成买菜清单
│   ├── services/               # 业务逻辑（可被 API / 定时任务 / CLI 复用）
│   │   ├── recipe_service.py
│   │   ├── scaling_service.py  # servings 缩放 / ratio 还原
│   │   ├── shopping_list_service.py
│   │   └── ai_service.py       # 大模型生成食谱，严格 JSON 校验后落库
│   └── db/
│       ├── init_db.py          # ★ 执行 schema.sql 建库（幂等，支持 --reset）
│       └── seed.py             # 演示数据（schema.sql 已内置种子，暂不需要）
│
├── scripts/
│   ├── verify_schema.py        # ★ schema.sql 自检：可执行/幂等/约束/触发器/视图
│   └── dev.ps1                 # ★ 一键建库 + 启动 uvicorn（-Port / -NoReload）
│
├── tests/                      # pytest：test_recipes_api.py、test_scaling.py ...
└── data/                       # 运行时生成 cook.db（已 gitignore，靠 schema.sql 重建）
```

## 分层约定

`api/ → services/ → models/ + schemas/`，**依赖只能单向向下**：

- `api/`：只做参数校验、鉴权、状态码映射，不写业务判断；
- `services/`：配比换算、份量缩放、成本估算、清单聚合等真实逻辑；
- `models/`：只做映射，不塞业务方法；`schema.sql` 才是建表事实来源。

## 快速自检

```powershell
python scripts/verify_schema.py     # 不依赖任何三方库，仅用标准库 sqlite3
python -m pytest                    # 接口冒烟：/health、CORS、启动建表、幂等
```

它会验证：SQL 可执行、可重复执行、3 张表 / 视图 / 9 个索引 / 2 个触发器就位、
11 条 CHECK/UNIQUE/外键约束真实生效、级联删除、updated_at 触发器、
成本视图口径合理、ratio 数据质量自洽。

## 数据库要点速览

| 表 | 作用 | 关键设计 |
| --- | --- | --- |
| `recipes` | 食谱主表 | `servings` 是份量换算锚点；`source` 区分 manual/ai |
| `ingredients` | 食材主数据 | `name` 唯一、`alias` 收敛别名、`reference_price` 为「元/default_unit」 |
| `recipe_ingredients` | 配料比例（多对多） | 同时支持 `quantity+unit`（绝对）、`ratio`（相对配比）、`适量`；部分唯一索引保证每食谱仅一个基准食材 |

## 路线图

1. ✅ **数据库结构 + 项目骨架**
2. ✅ **Web 框架**：`config.py` / `database.py` / `init_db.py` / `main.py`，`/health` 可用、启动自动建表、CORS 就绪
3. 食谱 / 食材的只读接口：`GET /api/v1/recipes`、`GET /api/v1/recipes/{id}`、`GET /api/v1/ingredients`
4. 写接口 + 份数换算服务（`GET /api/v1/recipes/{id}?servings=4`）
5. 买菜清单：多菜聚合、常备品跳过、成本预估
6. AI 生成食谱：`POST /api/v1/recipes/generate`（结构化 JSON → 校验 → 落库为 draft）
7. 单位换算表与比价接入，替换 `v_recipe_cost` 的静态参考价
