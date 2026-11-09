-- =============================================================================
--  AI 智能食谱买菜平台 · 数据库 Schema
--  版本   : v0.1 (MVP)
--  引擎   : SQLite 3.35+ (建议 3.38+，支持部分索引与 RETURNING)
--  字符集 : UTF-8
--  说明   :
--    1) 本文件是「唯一事实来源」，`app/db/init_db.py` 会直接执行它建库；
--       后续若引入 Alembic，则由 Alembic 接管迁移，此文件降级为初始化脚本。
--    2) 时间统一存 ISO8601 UTC 字符串，例如 2026-04-11T08:30:00.000Z，
--       不用 SQLite 的本地时间，避免跨时区/夏令时问题。
--    3) 金额/用量统一用 REAL；若后续要做精确财务计算，应改为「以分为单位的 INTEGER」。
-- =============================================================================

PRAGMA foreign_keys = ON;   -- SQLite 默认不启用外键，必须在每个连接上显式开启
PRAGMA journal_mode = WAL;  -- 读写并发更友好（FastAPI 多线程访问时收益明显）

-- -----------------------------------------------------------------------------
-- 【开发期重建开关】需要清库重来时才取消下面 4 行注释。
-- 生产环境请改用 Alembic 迁移，不要执行这部分。
-- -----------------------------------------------------------------------------
-- DROP VIEW  IF EXISTS v_recipe_cost;
-- DROP TABLE IF EXISTS recipe_ingredients;
-- DROP TABLE IF EXISTS ingredients;
-- DROP TABLE IF EXISTS recipes;


-- =============================================================================
-- 1) 食谱表 recipes
--    一条记录 = 一道可执行的菜谱。
-- =============================================================================
CREATE TABLE IF NOT EXISTS recipes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    title             TEXT    NOT NULL,                  -- 菜名，如「番茄炒蛋」
    summary           TEXT,                              -- 一句话简介 / AI 生成的推荐语
    cover_image_url   TEXT,                              -- 封面图地址

    cuisine           TEXT,                              -- 菜系：川菜 / 粤菜 / 家常 ...
    category          TEXT,                              -- 品类：家常菜 / 汤羹 / 主食 / 凉菜 / 甜品
    difficulty        INTEGER NOT NULL DEFAULT 1
                              CHECK (difficulty BETWEEN 1 AND 5),   -- 1=新手, 5=大师

    -- servings 是「配比 → 采购量」换算的锚点，也是成本估算的分母，不可为空
    servings          INTEGER NOT NULL DEFAULT 2
                              CHECK (servings > 0 AND servings <= 20),

    prep_time_minutes INTEGER NOT NULL DEFAULT 0
                              CHECK (prep_time_minutes >= 0),       -- 备菜耗时
    cook_time_minutes INTEGER NOT NULL DEFAULT 0
                              CHECK (cook_time_minutes >= 0),       -- 烹饪耗时

    -- 区分人工录入与 AI 生成，便于后续对 AI 结果做质量抽检/灰度
    source            TEXT    NOT NULL DEFAULT 'manual'
                              CHECK (source IN ('manual', 'ai')),
    status            TEXT    NOT NULL DEFAULT 'published'
                              CHECK (status IN ('draft', 'published', 'archived')),

    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (length(trim(title)) > 0)
);

-- 列表页最常见的查询：按状态 + 品类筛选，再按更新时间倒序
CREATE INDEX IF NOT EXISTS idx_recipes_status_category
    ON recipes (status, category, updated_at DESC);

-- 菜名模糊搜索（SQLite 的 LIKE '%xx%' 无法走索引，此处只为排序/去重服务；
-- 关键词检索量上来后可换 FTS5 虚拟表）
CREATE INDEX IF NOT EXISTS idx_recipes_title ON recipes (title);


-- =============================================================================
-- 2) 食材表 ingredients
--    全站食材主数据（Master Data）。买菜清单、价格预估、AI 归一化都以它为准。
--    「五花肉」「猪五花」「带皮五花」这类同物异名靠 alias 收敛，不在本表重复建档。
-- =============================================================================
CREATE TABLE IF NOT EXISTS ingredients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    name            TEXT    NOT NULL,                    -- 标准名，如「五花肉」
    -- 别名，多个用英文逗号分隔，如 '猪五花,带皮五花,三层肉'
    -- 用途：NL 搜索 / AI 识别结果落到标准食材上
    alias           TEXT,

    category        TEXT    NOT NULL DEFAULT '其他'
                            CHECK (category IN ('蔬菜', '肉类', '水产', '蛋奶',
                                                '豆制品', '主食', '调味料',
                                                '干货', '水果', '其他')),

    -- 计量单位：来自该食材最常见的采购口径（按个买的鸡蛋不该按克记）
    default_unit    TEXT    NOT NULL DEFAULT '克'
                            CHECK (default_unit IN ('克', '千克', '毫升', '升',
                                                    '个', '颗', '根', '片', '瓣',
                                                    '勺', '汤匙', '茶匙', '把',
                                                    '块', '包', '盒', '适量')),

    -- 参考单价，单位 = 元 / default_unit；仅用于购物车预估，真实价格在比价模块里
    reference_price REAL    CHECK (reference_price IS NULL OR reference_price >= 0),

    -- 1 = 常备品（盐、油、糖、生抽…），生成买菜清单时默认跳过
    is_common       INTEGER NOT NULL DEFAULT 0
                            CHECK (is_common IN (0, 1)),

    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (length(trim(name)) > 0)
);

-- 唯一索引而非列级 UNIQUE：SQLite 的 COLLATE NOCASE 只对 ASCII 生效，中文无所谓，
-- 但放在索引里方便后续换成「拼音/别名」函数索引而不改表结构。
CREATE UNIQUE INDEX IF NOT EXISTS uk_ingredients_name ON ingredients (name);

-- 按品类筛选食材库（录入食谱时的下拉依赖）
CREATE INDEX IF NOT EXISTS idx_ingredients_category ON ingredients (category, name);
CREATE INDEX IF NOT EXISTS idx_ingredients_common   ON ingredients (is_common);


-- =============================================================================
-- 3) 配料比例表 recipe_ingredients
--    食谱 ↔ 食材 的多对多桥梁表，每行 = 「某食谱用了某食材，用量/配比是多少」。
--
--    两种记法可同时存在，由前端按场景取用：
--      a) 绝对用量：quantity + unit        例：quantity=300, unit='克'
--      b) 相对配比：ratio（归一化，基准=1）  例：糖醋汁 醋2 : 糖1 : 生抽1
--                                             → ratio = 2.0 / 1.0 / 1.0
--      c) 「适量」：quantity 为 NULL，unit='适量'
--
--    为什么两者都留：用户按「几人份」换算时用 quantity；展示「黄金比例」、
--    以及 AI 生成可变份量的配方时用 ratio，避免反复做除法产生误差。
--
--    ⚠ ratio 的语义边界（重要，前端与 AI 生成都要遵守）：
--      ratio 只在「与基准食材同单位」的行上有意义。基准是 300 克番茄时，
--      鸡蛋 3 个不能写 ratio=0.5，因为 0.5 个基准换算不出「个」。
--      跨单位一律把 ratio 留 NULL，只用 quantity 参与份数缩放。
--      单位换算由后续的 unit_conversion 表统一处理，不要在本表里硬编码系数。
-- =============================================================================
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,

    recipe_id     INTEGER NOT NULL
                          REFERENCES recipes (id)     ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL
                          REFERENCES ingredients (id) ON DELETE RESTRICT,

    -- 分组，决定前端展示分区；同一食材允许在「腌料」和「点缀」各出现一次
    group_name    TEXT    NOT NULL DEFAULT '主料'
                          CHECK (group_name IN ('主料', '辅料', '调味料', '腌料', '点缀')),

    -- 用量对应 recipes.servings 人份；NULL 表示「适量/少许」
    quantity      REAL    CHECK (quantity IS NULL OR quantity > 0),
    unit          TEXT,                                  -- 与 ingredients.default_unit 同域

    -- 归一化比例，基准食材 = 1.0；NULL 表示该行不参与比例计算
    ratio         REAL    CHECK (ratio IS NULL OR ratio > 0),
    -- 1 = 该食谱的基准食材（每个食谱有且仅有 1 个，由下方部分唯一索引强制）
    is_base       INTEGER NOT NULL DEFAULT 0 CHECK (is_base IN (0, 1)),

    -- 1 = 可选（按口味增减 / 缺货可跳过），不算进必需食材校验
    is_optional   INTEGER NOT NULL DEFAULT 0 CHECK (is_optional IN (0, 1)),

    prep_note     TEXT,                                  -- 处理方式：去皮、切丝、提前泡发
    sort_order    INTEGER NOT NULL DEFAULT 0,            -- 同组内展示顺序

    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    -- 至少要有「用量」或「单位」之一（单位='适量' 的调料两条都没有用量）
    CHECK (quantity IS NOT NULL OR unit IS NOT NULL),
    -- 基准食材必须同时给出 ratio，否则比例无从计算
    CHECK (is_base = 0 OR ratio IS NOT NULL),
    -- 同一食谱的同一食材在同一分组内只允许一行；改用量请 UPDATE，不要插重复行
    CONSTRAINT uk_recipe_ingredient UNIQUE (recipe_id, ingredient_id, group_name)
);

-- 渲染一个食谱的全部配料（覆盖 API 最热的查询）
CREATE INDEX IF NOT EXISTS idx_ri_recipe     ON recipe_ingredients (recipe_id, group_name, sort_order);
-- 反向查询：某食材能做哪些菜 / 反向补全采购
CREATE INDEX IF NOT EXISTS idx_ri_ingredient ON recipe_ingredients (ingredient_id);
-- 保证「每个食谱最多一个基准食材」——SQLite 3.8+ 的部分索引
CREATE UNIQUE INDEX IF NOT EXISTS uk_ri_single_base
    ON recipe_ingredients (recipe_id) WHERE is_base = 1;


-- =============================================================================
-- 4) 触发器：自动维护 updated_at
--    SQLite 没有 ON UPDATE CURRENT_TIMESTAMP，用触发器兜底，
--    避免某处 UPDATE 忘了改时间导致缓存/增量同步失效。
--    （SQLite 默认递归触发器关闭，触发器内的 UPDATE 不会再次触发自身）
-- =============================================================================
CREATE TRIGGER IF NOT EXISTS trg_recipes_updated_at
AFTER UPDATE ON recipes
FOR EACH ROW
BEGIN
    UPDATE recipes
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_ingredients_updated_at
AFTER UPDATE ON ingredients
FOR EACH ROW
BEGIN
    UPDATE ingredients
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;


-- =============================================================================
-- 5) 视图：食谱成本估算
--    给「加入购物车/买菜清单」页提供预估金额与食材数量。
--
--    ⚠ 已知局限（下一阶段里程碑）：
--      - 未做单位换算，只有当 recipe_ingredients.unit 与 ingredients.default_unit
--        一致时结果才准确；否则需要新增 unit_conversion(食材, 源单位, 目标单位, 系数) 表。
--      - 未考虑「适量」调料（quantity 为 NULL，按 0 计）。
--      - reference_price 是静态参考价，接入比价/实时报价后应改为按门店取价。
-- =============================================================================
CREATE VIEW IF NOT EXISTS v_recipe_cost AS
SELECT r.id                                     AS recipe_id,
       r.title                                  AS title,
       r.servings                               AS servings,
       COUNT(ri.id)                             AS ingredient_rows,
       SUM(CASE WHEN ri.is_optional = 0 THEN 1 ELSE 0 END) AS required_rows,
       ROUND(SUM(COALESCE(ri.quantity, 0) * COALESCE(i.reference_price, 0)), 2)
                                                AS estimated_cost
  FROM recipes r
  LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
  LEFT JOIN ingredients        i  ON i.id         = ri.ingredient_id
 GROUP BY r.id, r.title, r.servings;


-- =============================================================================
-- 6) 示例数据（Seed）
--    仅用于本地开发/联调，生产环境请删掉本节。
--    全部使用 INSERT OR IGNORE + 显式主键，可重复执行而不会重复插入。
--
--    ⚠ reference_price 的口径是「元 / 该行的 default_unit」，别再按斤/千克的
--      直觉填数：番茄 6 元/斤 ≠ 6.00 元/克，应填 0.012（元/克）。
--      自检脚本会校验单份成本落在合理区间，填错会直接报错。
-- =============================================================================
INSERT OR IGNORE INTO ingredients
    (id, name, alias, category, default_unit, reference_price, is_common) VALUES
    (1,  '番茄',   '西红柿',          '蔬菜',   '克',   0.012, 0),  -- 约 6 元/斤
    (2,  '鸡蛋',   '土鸡蛋,洋鸡蛋',    '蛋奶',   '个',   1.200, 0),
    (3,  '小葱',   '香葱,葱花',        '蔬菜',   '根',   0.500, 0),
    (4,  '食用油', '花生油,菜籽油',    '调味料', '毫升', 0.010, 1),  -- 约 10 元/升
    (5,  '盐',     '食盐,精盐',        '调味料', '克',   0.003, 1),  -- 约 3 元/千克
    (6,  '白砂糖', '白糖,糖',          '调味料', '克',   0.006, 1),
    (7,  '土豆',   '马铃薯,洋芋',      '蔬菜',   '克',   0.006, 0),  -- 约 6 元/千克
    (8,  '青椒',   '灯笼椒,柿子椒',    '蔬菜',   '克',   0.012, 0),  -- 约 12 元/千克
    (9,  '米醋',   '白醋,陈醋',        '调味料', '毫升', 0.008, 1),  -- 约 8 元/升
    (10, '大蒜',   '蒜,蒜瓣',          '蔬菜',   '瓣',   0.300, 1);

INSERT OR IGNORE INTO recipes
    (id, title, summary, cuisine, category, difficulty, servings,
     prep_time_minutes, cook_time_minutes, source, status) VALUES
    (1, '番茄炒蛋', '国民下饭菜，酸甜开胃，10 分钟搞定。',
     '家常', '家常菜', 1, 2, 5, 5, 'manual', 'published'),
    (2, '青椒土豆丝', '大火快炒，脆爽不粘锅，关键是冲洗淀粉。',
     '川菜', '家常菜', 2, 2, 10, 6, 'ai', 'published');

-- 番茄炒蛋：以番茄为基准（ratio=1）。
-- 鸡蛋记的是「3 个」而基准是「克」，跨单位，ratio 无法表达 → 留 NULL，
-- 只用 quantity 参与份数换算（严禁为了"看起来完整"硬填一个 0.5）。
INSERT OR IGNORE INTO recipe_ingredients
    (recipe_id, ingredient_id, group_name, quantity, unit, ratio, is_base, is_optional, prep_note, sort_order) VALUES
    (1, 1, '主料',   300,  '克',   1.0,  1, 0, '切滚刀块',        1),
    (1, 2, '主料',   3,    '个',   NULL, 0, 0, '打散加少许盐',    2),
    (1, 3, '点缀',   NULL, '适量', NULL, 0, 1, '切葱花',          3),
    (1, 4, '调味料', 20,   '毫升', NULL, 0, 0, NULL,              1),
    (1, 5, '调味料', 3,    '克',   NULL, 0, 0, NULL,              2),
    (1, 6, '调味料', 5,    '克',   NULL, 0, 1, '中和番茄酸味',    3);

-- 青椒土豆丝：以土豆为基准，青椒 0.25（400g : 100g）
INSERT OR IGNORE INTO recipe_ingredients
    (recipe_id, ingredient_id, group_name, quantity, unit, ratio, is_base, is_optional, prep_note, sort_order) VALUES
    (2, 7,  '主料',   400,  '克',   1.00, 1, 0, '切细丝后清水冲去淀粉', 1),
    (2, 8,  '辅料',   100,  '克',   0.25, 0, 0, '去籽切丝',             1),
    (2, 10, '调味料', 2,    '瓣',   NULL, 0, 0, '拍碎',                 1),
    (2, 9,  '调味料', 15,   '毫升', NULL, 0, 0, '出锅前淋入',           2),
    (2, 5,  '调味料', 3,    '克',   NULL, 0, 0, NULL,                   3),
    (2, 4,  '调味料', 20,   '毫升', NULL, 0, 0, NULL,                   4);


-- =============================================================================
-- 7) 后续里程碑预留（本版本不创建，仅记录设计方向，勿取消注释）
-- =============================================================================
-- recipe_steps      (id, recipe_id, step_no, instruction, image_url, duration_seconds)
--                   烹饪步骤，step_no 与 recipe_id 组唯一
-- shopping_lists    (id, user_id, title, status, created_at)
-- shopping_items    (id, list_id, ingredient_id, quantity, unit, checked, source_recipe_id)
--                   买菜清单；quantity 由 recipe_ingredients按份数换算后聚合而来
-- users             (id, nickname, phone, created_at)  —— 先做单机版可省略
-- unit_conversion   (ingredient_id, from_unit, to_unit, factor)
--                   单位换算，解决 v_recipe_cost 的精度局限



